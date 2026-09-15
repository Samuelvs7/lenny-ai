"""In-process dense vector index.

**Why not pgvector.** pgvector is not present in stock PostgreSQL. Requiring it
would mean the application only runs on a Postgres someone remembered to
install an extension into — breaking the "clone it and run it" promise on a
plain local server, and adding a failure mode to every deployment target.

Instead, vectors live in Postgres as the source of truth and are loaded once at
startup into a single NumPy matrix. Search is one matrix-vector product.

**Where this stops working**, stated plainly so the next engineer does not have
to discover it: memory is ``chunks x dims x 4`` bytes — the full 269-episode
archive is roughly 30k chunks x 768 dims ≈ 92 MB, and query time stays in
single-digit milliseconds. Beyond ~100k chunks, or the moment you run more than
one API replica (each would hold its own copy and reload on deploy), move to
pgvector with an HNSW index. The swap is contained: implement ``search`` against
SQL and delete this file. Nothing above the retrieval package changes.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.observability import get_logger, log_duration

log = get_logger(__name__)


@dataclass(slots=True)
class DenseHit:
    chunk_id: int
    score: float


class DenseIndex:
    """Cosine-similarity search over chunk embeddings.

    Vectors are L2-normalised at load time, so cosine similarity is a plain dot
    product and the per-query cost is one BLAS call.
    """

    def __init__(self) -> None:
        self._matrix: np.ndarray | None = None
        self._chunk_ids: np.ndarray | None = None
        self._dimensions: int = 0
        self._model: str | None = None
        self._lock = asyncio.Lock()

    @property
    def is_ready(self) -> bool:
        return self._matrix is not None and self._matrix.shape[0] > 0

    @property
    def size(self) -> int:
        return 0 if self._matrix is None else int(self._matrix.shape[0])

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model(self) -> str | None:
        return self._model

    async def load(self, engine: AsyncEngine) -> int:
        """(Re)load all embedded chunks from Postgres. Returns the row count."""
        async with self._lock:
            with log_duration(log, "retrieval.index_load") as span:
                async with engine.connect() as conn:
                    rows = (
                        await conn.execute(
                            text(
                                """
                                SELECT id, embedding, embedding_dim, embedding_model
                                FROM chunks
                                WHERE embedding IS NOT NULL
                                ORDER BY id
                                """
                            )
                        )
                    ).fetchall()

                if not rows:
                    self._matrix = None
                    self._chunk_ids = None
                    span["chunks"] = 0
                    span["outcome"] = "empty"
                    return 0

                dimensions = rows[0][2] or (len(rows[0][1]) // 4)
                models = {row[3] for row in rows if row[3]}

                # A mixed-model index silently degrades retrieval: vectors from
                # different models are not comparable. Warn loudly — the fix is
                # a re-ingest, and the symptom otherwise looks like "the search
                # is just bad".
                if len(models) > 1:
                    log.warning(
                        "retrieval.mixed_embedding_models",
                        models=sorted(models),
                        action="re-run ingestion with a single OLLAMA_EMBED_MODEL",
                    )

                usable = [row for row in rows if len(row[1]) // 4 == dimensions]
                if len(usable) != len(rows):
                    log.warning(
                        "retrieval.dimension_mismatch",
                        expected=dimensions,
                        dropped=len(rows) - len(usable),
                    )

                matrix = np.empty((len(usable), dimensions), dtype=np.float32)
                chunk_ids = np.empty(len(usable), dtype=np.int64)
                for position, row in enumerate(usable):
                    matrix[position] = struct.unpack(f"<{dimensions}f", row[1])
                    chunk_ids[position] = row[0]

                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                norms[norms == 0] = 1.0  # a zero vector must not produce NaN
                matrix /= norms

                self._matrix = matrix
                self._chunk_ids = chunk_ids
                self._dimensions = dimensions
                self._model = sorted(models)[0] if models else None

                span["chunks"] = len(usable)
                span["dimensions"] = dimensions
                span["memory_mb"] = round(matrix.nbytes / 1_048_576, 1)
                return len(usable)

    def search(self, query_vector: list[float], *, limit: int) -> list[DenseHit]:
        """Top-``limit`` chunks by cosine similarity."""
        if self._matrix is None or self._chunk_ids is None:
            return []

        vector = np.asarray(query_vector, dtype=np.float32)
        if vector.shape[0] != self._dimensions:
            log.warning(
                "retrieval.query_dimension_mismatch",
                query_dims=int(vector.shape[0]),
                index_dims=self._dimensions,
                hint="OLLAMA_EMBED_MODEL differs from the model used at ingestion time",
            )
            return []

        norm = float(np.linalg.norm(vector))
        if norm == 0:
            return []
        vector /= norm

        scores = self._matrix @ vector
        limit = min(limit, scores.shape[0])
        # argpartition finds the top-k without sorting the whole array.
        top = np.argpartition(-scores, limit - 1)[:limit]
        top = top[np.argsort(-scores[top])]

        return [
            DenseHit(chunk_id=int(self._chunk_ids[i]), score=float(scores[i])) for i in top
        ]
