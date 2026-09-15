"""Hybrid retrieval: Postgres full-text + dense vectors, fused with RRF.

**Why hybrid rather than vectors alone.** This corpus is dense with jargon and
proper nouns — "PMF", "ICP", "activation", "Superhuman", "Rippling", specific
guest names. Embeddings blur exactly those: asked about "Airbnb's pricing",
a vector-only search happily returns generic pricing talk from other episodes.
Lexical search nails the rare term; embeddings catch the paraphrase ("how do I
know when I've found product-market fit" -> a passage that never says "PMF").
Each covers the other's blind spot.

**Why Reciprocal Rank Fusion.** The two arms produce incomparable scores —
``ts_rank_cd`` is unbounded, cosine is [-1, 1]. Normalising them into a shared
scale requires tuning constants that drift with the corpus. RRF ignores score
magnitude and fuses on *rank*, which is robust and has one parameter:

    score(d) = Σ  1 / (k + rank_i(d))

``k = 60`` is the standard value from the original paper; it damps the
influence of any single arm's top result enough that one bad match cannot
dominate the fused list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.domain import EpisodeRef, RetrievedChunk
from app.errors import ProviderError
from app.observability import get_logger, log_duration
from app.providers.base import EmbeddingProvider
from app.retrieval.index import DenseIndex

log = get_logger(__name__)

#: RRF damping constant (Cormack et al.). Higher flattens the contribution of
#: top ranks; 60 is the widely replicated default.
RRF_K = 60

#: Words carrying no retrieval signal. Postgres' English dictionary already
#: drops most of these inside to_tsquery, but filtering here keeps the query
#: string short and stops question scaffolding ("what did guests say about")
#: from dominating a disjunctive query.
_QUERY_STOPWORDS = frozenset(
    """
    a about all also am an and any are as at be been but by can cant could did
    do does doing dont for from had has have he her hers him his how i if in
    into is it its just me more most my no nor not of on once only or other our
    out over own same she should so some such than that the their them then
    there these they this those through to too under until up us very was we
    were what when where which while who whom why will with would you your
    say says said tell talk talks talked discuss discussed mention mentioned
    guest guests episode episodes podcast lenny
    """.split()
)

#: Terms shorter than this rarely disambiguate anything, with an allow-list for
#: the acronyms this corpus is full of.
_MIN_TERM_LENGTH = 3
_SHORT_TERM_ALLOWLIST = frozenset({"b2b", "b2c", "pmf", "icp", "ltv", "cac", "nps", "arr", "mrr", "ai", "ux", "kpi", "roi", "seo"})

_TERM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")


def build_or_tsquery(query: str) -> str:
    """Build a disjunctive ``tsquery`` string from free text.

    Terms are extracted, filtered, and escaped. Escaping matters: user input
    reaches ``to_tsquery``, which has its own syntax (``&``, ``|``, ``!``,
    ``:``, parentheses) and raises on malformed input. We never interpolate raw
    user text — terms are reduced to alphanumerics and quoted.
    """
    terms: list[str] = []
    seen: set[str] = set()

    for match in _TERM_PATTERN.finditer(query.lower()):
        token = match.group(0).strip("'’-")
        if not token or token in seen:
            continue
        if token in _QUERY_STOPWORDS:
            continue
        if len(token) < _MIN_TERM_LENGTH and token not in _SHORT_TERM_ALLOWLIST:
            continue
        # Hyphenated phrases ("product-market") are kept whole: Postgres indexes
        # both the compound and its parts, so the compound is the stronger hit.
        safe = re.sub(r"[^a-z0-9-]", "", token)
        if not safe or safe == "-":
            continue
        seen.add(token)
        terms.append(f"'{safe}'")

    return " | ".join(terms[:24])


@dataclass(slots=True)
class RetrievalResult:
    """Chunks plus enough diagnostics to explain the outcome."""

    chunks: list[RetrievedChunk] = field(default_factory=list)
    lexical_count: int = 0
    dense_count: int = 0
    dense_available: bool = False
    #: Highest fused RRF score. Good for *ordering*, useless as a confidence
    #: measure — see :meth:`is_well_grounded`.
    top_score: float = 0.0
    #: Highest raw cosine similarity from the dense arm, in [-1, 1]. This is an
    #: absolute measure of how close the best passage actually is, which is what
    #: the grounding decision needs.
    top_similarity: float = 0.0
    #: True when EVERY content term in the query co-occurs in one passage
    #: (a conjunctive match). This is the *strong* lexical signal. The ordinary
    #: `lexical_count` above comes from a disjunctive query and is near-always
    #: non-zero — "sourdough bread" matches 40 passages on the words "best" and
    #: "starter" alone — so it says nothing about relevance and must never be
    #: used as a confidence signal.
    strong_lexical_match: bool = False
    query: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.chunks

    def is_well_grounded(self, min_similarity: float) -> bool:
        """Is there genuinely enough evidence here to answer from?

        **Why this is not a threshold on the fused score.** RRF scores encode
        rank, not relevance: the dense arm always returns its top-k, so its
        best hit scores ``1/(60+1) = 0.0164`` whether the passage is a perfect
        match or nonsense. Thresholding that measures "did retrieval return
        anything", which is always true — the assistant would never decline.

        Cosine similarity *is* absolute, so it is the gate. A lexical match is
        accepted as independent evidence because exact term overlap ("Superhuman",
        "PMF") is strong signal that embeddings sometimes rate as mediocre.
        """
        if not self.chunks:
            return False
        if self.strong_lexical_match:
            return True
        return self.top_similarity >= min_similarity


class HybridRetriever:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        settings: Settings,
        dense_index: DenseIndex,
        embedder: EmbeddingProvider | None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._index = dense_index
        self._embedder = embedder

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        candidates: int | None = None,
    ) -> RetrievalResult:
        """Retrieve passages relevant to ``query``."""
        query = " ".join(query.split())
        if not query:
            return RetrievalResult(query=query)

        top_k = limit or self._settings.retrieval_top_k
        candidate_count = candidates or self._settings.retrieval_candidates

        with log_duration(log, "retrieval.search", query_chars=len(query)) as span:
            lexical = await self._lexical_search(query, candidate_count)
            dense = await self._dense_search(query, candidate_count)

            span["lexical_hits"] = len(lexical)
            span["dense_hits"] = len(dense)

            fused = self._fuse(lexical, dense)
            chunk_ids = [chunk_id for chunk_id, _ in fused[:top_k]]
            hydrated = await self._hydrate(chunk_ids)

            ranks_lexical = {cid: i + 1 for i, cid in enumerate(lexical)}
            ranks_dense = {hit.chunk_id: i + 1 for i, hit in enumerate(dense)}
            scores = dict(fused)

            chunks: list[RetrievedChunk] = []
            for chunk_id in chunk_ids:
                chunk = hydrated.get(chunk_id)
                if chunk is None:
                    continue
                chunk.score = round(scores.get(chunk_id, 0.0), 6)
                chunk.lexical_rank = ranks_lexical.get(chunk_id)
                chunk.dense_rank = ranks_dense.get(chunk_id)
                chunks.append(chunk)

            top_similarity = max((hit.score for hit in dense), default=0.0)
            strong_lexical = await self._strong_lexical_match(query)

            span["returned"] = len(chunks)
            span["top_score"] = round(chunks[0].score, 6) if chunks else 0.0
            span["top_similarity"] = round(top_similarity, 4)
            span["strong_lexical"] = strong_lexical

            return RetrievalResult(
                chunks=chunks,
                lexical_count=len(lexical),
                dense_count=len(dense),
                dense_available=self._index.is_ready,
                top_score=chunks[0].score if chunks else 0.0,
                top_similarity=top_similarity,
                strong_lexical_match=strong_lexical,
                query=query,
            )

    # --- arms ---------------------------------------------------------------

    async def _lexical_search(self, query: str, limit: int) -> list[int]:
        """Postgres full-text search, best first.

        **Uses OR, not AND.** ``websearch_to_tsquery`` and ``plainto_tsquery``
        both conjoin every term, so a natural-language question matches
        nothing: "What did guests say about finding product-market fit?"
        becomes ``guest & say & find & product-market & fit`` and returns zero
        rows against a corpus that plainly discusses product-market fit. That
        was not hypothetical — it silently disabled the lexical arm entirely
        until it showed up as ``lexical_hits: 0`` in the logs.

        Disjunction restores recall; ``ts_rank_cd`` restores precision by
        ranking passages that match more of the query terms higher.
        """
        tsquery = build_or_tsquery(query)
        if not tsquery:
            return []

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT c.id
                        FROM chunks c,
                             to_tsquery('english', :tsquery) AS q
                        WHERE c.tsv @@ q
                        ORDER BY ts_rank_cd(c.tsv, q) DESC, c.id
                        LIMIT :limit
                        """
                    ),
                    {"tsquery": tsquery, "limit": limit},
                )
            ).fetchall()
        return [row[0] for row in rows]

    async def _strong_lexical_match(self, query: str) -> bool:
        """Does any single passage contain *all* the query's content terms?

        Conjunctive, unlike the retrieval query. Used only as a confidence
        signal: if a passage literally contains every meaningful word of the
        question, the archive demonstrably covers it, even when the embedding
        similarity is unremarkable.
        """
        async with self._engine.connect() as conn:
            found = (
                await conn.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM chunks c
                            WHERE c.tsv @@ websearch_to_tsquery('english', :query)
                        )
                        """
                    ),
                    {"query": query},
                )
            ).scalar_one()
        return bool(found)

    async def _dense_search(self, query: str, limit: int):
        """Embed the query and search the in-memory index.

        Returns an empty list — never raises — when embeddings are unavailable.
        Lexical-only retrieval is a degraded but working system, and taking the
        whole request down because Ollama is busy would be the wrong trade.
        """
        if self._embedder is None or not self._index.is_ready:
            return []
        try:
            vectors = await self._embedder.embed([query])
        except ProviderError as exc:
            log.warning("retrieval.query_embedding_failed", error=exc.code, detail=exc.detail)
            return []
        if not vectors:
            return []
        return self._index.search(vectors[0], limit=limit)

    # --- fusion -------------------------------------------------------------

    @staticmethod
    def _fuse(lexical_ids: list[int], dense_hits) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for rank, chunk_id in enumerate(lexical_ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        for rank, hit in enumerate(dense_hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)

    async def _hydrate(self, chunk_ids: list[int]) -> dict[int, RetrievedChunk]:
        """Load chunk text and episode metadata for the fused winners."""
        if not chunk_ids:
            return {}

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT c.id, c.content, c.speaker, c.start_seconds, c.end_seconds,
                               e.slug, e.guest, e.title, e.youtube_url, e.publish_date
                        FROM chunks c
                        JOIN episodes e ON e.id = c.episode_id
                        WHERE c.id = ANY(:chunk_ids)
                        """
                    ),
                    {"chunk_ids": chunk_ids},
                )
            ).mappings().all()

        return {
            row["id"]: RetrievedChunk(
                chunk_id=row["id"],
                content=row["content"],
                speaker=row["speaker"],
                start_seconds=row["start_seconds"],
                end_seconds=row["end_seconds"],
                episode=EpisodeRef(
                    slug=row["slug"],
                    guest=row["guest"],
                    title=row["title"],
                    youtube_url=row["youtube_url"],
                    publish_date=row["publish_date"],
                ),
            )
            for row in rows
        }
