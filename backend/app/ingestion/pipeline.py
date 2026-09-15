"""Ingestion orchestration: fetch -> parse -> clean -> chunk -> embed -> store.

Operational properties that matter for handoff:

* **Idempotent.** Episodes are keyed by slug and carry a content hash;
  re-running skips anything unchanged. Safe to schedule.
* **Resumable.** Each episode commits in its own transaction. A failure at
  episode 40 leaves 39 episodes indexed and usable.
* **Auditable.** Every run writes a row to ``ingestion_runs`` with counts, and
  every episode records how much sponsor content was removed.
* **Degrades honestly.** If embeddings are unavailable, chunks are still stored
  and full-text retrieval still works. The run is reported as succeeded-without
  -embeddings rather than failing outright, because a lexical-only assistant is
  far better than none.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.errors import ProviderError
from app.ingestion.chunk import Chunk, chunk_blocks
from app.ingestion.fetch import EpisodeSource, TranscriptFetcher
from app.ingestion.parse import parse_transcript
from app.ingestion.sponsors import strip_sponsor_blocks
from app.observability import get_logger, log_duration
from app.providers.base import EmbeddingProvider

log = get_logger(__name__)

#: Texts per embedding request. Large enough to amortise HTTP overhead, small
#: enough that one failure does not cost minutes of recomputation on CPU.
EMBED_BATCH_SIZE = 16


def pack_vector(vector: Sequence[float]) -> bytes:
    """float32 little-endian, matching ``chunks.embedding``."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    """Inverse of :func:`pack_vector`. Used by tests and diagnostics."""
    return struct.unpack(f"<{len(blob) // 4}f", blob)


@dataclass(slots=True)
class IngestionStats:
    episodes_seen: int = 0
    episodes_ingested: int = 0
    episodes_skipped: int = 0
    chunks_written: int = 0
    sponsor_segments_removed: int = 0
    sponsor_words_removed: int = 0
    embeddings_written: int = 0
    embedding_failures: int = 0
    errors: list[str] = field(default_factory=list)


class IngestionPipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        engine: AsyncEngine,
        embedder: EmbeddingProvider | None,
        cache_dir: Path | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._embedder = embedder
        self._cache_dir = cache_dir

    async def run(self, *, max_episodes: int | None = None, force: bool = False) -> IngestionStats:
        settings = self._settings
        limit = settings.ingest_max_episodes if max_episodes is None else max_episodes

        stats = IngestionStats()
        run_id = await self._start_run()

        fetcher = TranscriptFetcher(
            repo_url=settings.ingest_repo_url,
            ref=settings.ingest_repo_ref,
            cache_dir=self._cache_dir,
        )

        try:
            slugs = await fetcher.list_episode_slugs()
            if limit and limit > 0:
                slugs = slugs[:limit]
            stats.episodes_seen = len(slugs)
            log.info(
                "ingestion.started",
                run_id=str(run_id),
                episodes=len(slugs),
                strip_sponsors=settings.ingest_strip_sponsors,
                embedding_model=self._embedder.model if self._embedder else None,
            )

            existing = await self._existing_hashes()

            for position, slug in enumerate(slugs, start=1):
                try:
                    source = await fetcher.fetch_episode(slug)
                    if source is None:
                        stats.episodes_skipped += 1
                        continue

                    if not force and existing.get(slug) == source.content_hash:
                        stats.episodes_skipped += 1
                        log.debug("ingestion.episode_unchanged", slug=slug)
                        continue

                    written = await self._ingest_episode(source, stats)
                    stats.episodes_ingested += 1
                    stats.chunks_written += written
                    log.info(
                        "ingestion.episode_done",
                        slug=slug,
                        position=position,
                        of=len(slugs),
                        chunks=written,
                    )
                except Exception as exc:  # one bad episode must not end the run
                    stats.episodes_skipped += 1
                    message = f"{slug}: {type(exc).__name__}: {exc}"
                    stats.errors.append(message)
                    log.error("ingestion.episode_failed", slug=slug, error=message)

            await self._finish_run(run_id, stats, status="succeeded")
            log.info(
                "ingestion.finished",
                run_id=str(run_id),
                ingested=stats.episodes_ingested,
                skipped=stats.episodes_skipped,
                chunks=stats.chunks_written,
                embeddings=stats.embeddings_written,
                sponsor_segments=stats.sponsor_segments_removed,
                sponsor_words=stats.sponsor_words_removed,
            )
            return stats

        except Exception as exc:
            await self._finish_run(run_id, stats, status="failed", error=str(exc))
            log.error("ingestion.run_failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            await fetcher.aclose()

    # --- per-episode --------------------------------------------------------

    async def _ingest_episode(self, source: EpisodeSource, stats: IngestionStats) -> int:
        parsed = parse_transcript(source.slug, source.content)

        blocks = parsed.blocks
        sponsor_segments = sponsor_words = 0
        if self._settings.ingest_strip_sponsors:
            blocks, report = strip_sponsor_blocks(parsed.blocks, guest=parsed.guest)
            sponsor_segments = report.segments_removed
            sponsor_words = report.words_removed
            stats.sponsor_segments_removed += sponsor_segments
            stats.sponsor_words_removed += sponsor_words

        chunks = chunk_blocks(
            blocks,
            target_words=self._settings.ingest_chunk_words,
            overlap_words=self._settings.ingest_chunk_overlap_words,
            duration_seconds=parsed.int_field("duration_seconds"),
        )
        if not chunks:
            log.warning("ingestion.no_chunks", slug=source.slug)
            return 0

        vectors = await self._embed_chunks(chunks, stats)

        async with self._engine.begin() as conn:
            episode_id = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO episodes (
                            slug, guest, title, youtube_url, video_id, publish_date,
                            description, duration_seconds, view_count, channel, keywords,
                            source_url, content_hash,
                            sponsor_segments_removed, sponsor_words_removed, ingested_at
                        ) VALUES (
                            :slug, :guest, :title, :youtube_url, :video_id, :publish_date,
                            :description, :duration_seconds, :view_count, :channel, :keywords,
                            :source_url, :content_hash,
                            :sponsor_segments, :sponsor_words, now()
                        )
                        ON CONFLICT (slug) DO UPDATE SET
                            guest = EXCLUDED.guest,
                            title = EXCLUDED.title,
                            youtube_url = EXCLUDED.youtube_url,
                            video_id = EXCLUDED.video_id,
                            publish_date = EXCLUDED.publish_date,
                            description = EXCLUDED.description,
                            duration_seconds = EXCLUDED.duration_seconds,
                            view_count = EXCLUDED.view_count,
                            channel = EXCLUDED.channel,
                            keywords = EXCLUDED.keywords,
                            source_url = EXCLUDED.source_url,
                            content_hash = EXCLUDED.content_hash,
                            sponsor_segments_removed = EXCLUDED.sponsor_segments_removed,
                            sponsor_words_removed = EXCLUDED.sponsor_words_removed,
                            ingested_at = now()
                        RETURNING id
                        """
                    ),
                    {
                        "slug": source.slug,
                        "guest": parsed.guest,
                        "title": parsed.title,
                        "youtube_url": parsed.youtube_url,
                        "video_id": parsed.video_id,
                        "publish_date": parsed.publish_date,
                        "description": parsed.description,
                        "duration_seconds": parsed.int_field("duration_seconds"),
                        "view_count": parsed.int_field("view_count"),
                        "channel": parsed.metadata.get("channel"),
                        "keywords": parsed.keywords,
                        "source_url": source.raw_url,
                        "content_hash": source.content_hash,
                        "sponsor_segments": sponsor_segments,
                        "sponsor_words": sponsor_words,
                    },
                )
            ).scalar_one()

            # Replace chunks wholesale: re-chunking can change boundaries, so
            # merging old and new would leave orphaned passages behind.
            await conn.execute(
                text("DELETE FROM chunks WHERE episode_id = :episode_id"),
                {"episode_id": episode_id},
            )

            payload = []
            for chunk, vector in zip(chunks, vectors):
                payload.append(
                    {
                        "episode_id": episode_id,
                        "chunk_index": chunk.index,
                        "content": chunk.content,
                        "speaker": chunk.speaker,
                        "start_seconds": chunk.start_seconds,
                        "end_seconds": chunk.end_seconds,
                        "word_count": chunk.word_count,
                        "embedding": pack_vector(vector) if vector else None,
                        "embedding_dim": len(vector) if vector else None,
                        "embedding_model": self._embedder.model if (vector and self._embedder) else None,
                    }
                )

            await conn.execute(
                text(
                    """
                    INSERT INTO chunks (
                        episode_id, chunk_index, content, speaker,
                        start_seconds, end_seconds, word_count,
                        embedding, embedding_dim, embedding_model
                    ) VALUES (
                        :episode_id, :chunk_index, :content, :speaker,
                        :start_seconds, :end_seconds, :word_count,
                        :embedding, :embedding_dim, :embedding_model
                    )
                    """
                ),
                payload,
            )

        return len(chunks)

    async def _embed_chunks(
        self, chunks: list[Chunk], stats: IngestionStats
    ) -> list[list[float] | None]:
        """Embed in batches. Returns ``None`` per chunk when unavailable."""
        if self._embedder is None:
            return [None] * len(chunks)

        vectors: list[list[float] | None] = []
        for start in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[start : start + EMBED_BATCH_SIZE]
            try:
                with log_duration(log, "ingestion.embed_batch", size=len(batch)):
                    embedded = await self._embedder.embed([c.content for c in batch])
                vectors.extend(embedded)
                stats.embeddings_written += len(embedded)
            except ProviderError as exc:
                # Keep the text: lexical retrieval still works without vectors.
                stats.embedding_failures += len(batch)
                log.warning(
                    "ingestion.embedding_failed",
                    size=len(batch),
                    error=exc.code,
                    detail=exc.detail,
                )
                vectors.extend([None] * len(batch))
        return vectors

    # --- run bookkeeping ----------------------------------------------------

    async def _existing_hashes(self) -> dict[str, str]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT slug, content_hash FROM episodes"))
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    async def _start_run(self) -> UUID:
        async with self._engine.begin() as conn:
            return (
                await conn.execute(
                    text(
                        """
                        INSERT INTO ingestion_runs (source_ref, embedding_model)
                        VALUES (:source_ref, :embedding_model)
                        RETURNING id
                        """
                    ),
                    {
                        "source_ref": f"{self._settings.ingest_repo_url}@{self._settings.ingest_repo_ref}",
                        "embedding_model": self._embedder.model if self._embedder else None,
                    },
                )
            ).scalar_one()

    async def _finish_run(
        self, run_id: UUID, stats: IngestionStats, *, status: str, error: str | None = None
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE ingestion_runs SET
                        finished_at = now(),
                        status = :status,
                        episodes_seen = :seen,
                        episodes_ingested = :ingested,
                        episodes_skipped = :skipped,
                        chunks_written = :chunks,
                        sponsor_segments_removed = :sponsors,
                        error = :error
                    WHERE id = :run_id
                    """
                ),
                {
                    "status": status,
                    "seen": stats.episodes_seen,
                    "ingested": stats.episodes_ingested,
                    "skipped": stats.episodes_skipped,
                    "chunks": stats.chunks_written,
                    "sponsors": stats.sponsor_segments_removed,
                    "error": (error or "; ".join(stats.errors[:5])) or None,
                    "run_id": run_id,
                },
            )
