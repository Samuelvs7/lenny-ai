"""Ingestion CLI.

    python -m app.ingestion                 # ingest INGEST_MAX_EPISODES
    python -m app.ingestion --episodes 10   # override the count
    python -m app.ingestion --all           # every episode in the archive
    python -m app.ingestion --force         # re-ingest even if unchanged
    python -m app.ingestion --stats         # report index state and exit
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db.engine import dispose_engine, get_engine, run_migrations
from app.errors import AppError
from app.ingestion.pipeline import IngestionPipeline
from app.observability import configure_logging, get_logger
from app.providers.registry import ProviderRegistry

log = get_logger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"


async def _print_stats(engine) -> None:
    async with engine.connect() as conn:
        episodes = (await conn.execute(text("SELECT COUNT(*) FROM episodes"))).scalar_one()
        chunks = (await conn.execute(text("SELECT COUNT(*) FROM chunks"))).scalar_one()
        embedded = (
            await conn.execute(text("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"))
        ).scalar_one()
        sponsors = (
            await conn.execute(
                text("SELECT COALESCE(SUM(sponsor_segments_removed),0), "
                     "COALESCE(SUM(sponsor_words_removed),0) FROM episodes")
            )
        ).first()
        last_run = (
            await conn.execute(
                text(
                    "SELECT status, finished_at, episodes_ingested, chunks_written, embedding_model "
                    "FROM ingestion_runs ORDER BY started_at DESC LIMIT 1"
                )
            )
        ).first()

    print("\nKnowledge base")
    print(f"  episodes indexed     : {episodes}")
    print(f"  chunks               : {chunks}")
    print(f"  chunks with vectors  : {embedded}"
          f"{' (dense retrieval disabled)' if embedded == 0 else ''}")
    print(f"  sponsor segments cut : {sponsors[0]} ({sponsors[1]} words)")
    if last_run:
        print(f"  last run             : {last_run[0]} at {last_run[1]} "
              f"({last_run[2]} episodes, {last_run[3]} chunks, model={last_run[4]})")
    else:
        print("  last run             : never")
    print()


async def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion")
    parser.add_argument("--episodes", type=int, default=None,
                        help="Number of episodes to ingest (overrides INGEST_MAX_EPISODES).")
    parser.add_argument("--all", action="store_true", help="Ingest every episode.")
    parser.add_argument("--force", action="store_true",
                        help="Re-ingest episodes even if their content is unchanged.")
    parser.add_argument("--no-embeddings", action="store_true",
                        help="Skip embeddings; store text for full-text retrieval only.")
    parser.add_argument("--stats", action="store_true",
                        help="Print knowledge-base statistics and exit.")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings)
    engine = get_engine(settings)

    try:
        await run_migrations(engine)

        if args.stats:
            await _print_stats(engine)
            return 0

        registry = ProviderRegistry(settings)
        embedder = None if args.no_embeddings else registry.get_embedder()

        if embedder is not None:
            health = await embedder.health()
            if not health.available:
                # Do not fail: text-only ingestion still produces a working
                # lexical index, and the operator is told exactly what is lost.
                log.warning(
                    "ingestion.embeddings_unavailable",
                    model=health.model,
                    detail=health.detail,
                    consequence="continuing without vectors; retrieval will be lexical-only",
                )
                embedder = None

        max_episodes = 0 if args.all else args.episodes
        pipeline = IngestionPipeline(
            settings=settings, engine=engine, embedder=embedder, cache_dir=CACHE_DIR
        )
        stats = await pipeline.run(max_episodes=max_episodes, force=args.force)

        await _print_stats(engine)
        if stats.errors:
            print(f"Completed with {len(stats.errors)} episode error(s):")
            for message in stats.errors[:10]:
                print(f"  - {message}")
        await registry.aclose()
        return 0

    except AppError as exc:
        print(f"\nIngestion failed: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"Detail: {exc.detail}", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
