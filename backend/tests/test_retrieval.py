"""Retrieval: query building, fusion, grounding decisions, dense index."""

from __future__ import annotations

import numpy as np
import pytest

from app.domain import EpisodeRef, RetrievedChunk
from app.retrieval.hybrid import RRF_K, HybridRetriever, RetrievalResult, build_or_tsquery
from app.retrieval.index import DenseHit, DenseIndex


class TestQueryBuilding:
    def test_builds_a_disjunctive_query(self):
        """REGRESSION: conjunctive queries matched nothing.

        `websearch_to_tsquery` ANDs every term, so a natural question produced
        `guest & say & find & product-market & fit` and returned zero rows —
        silently disabling the lexical arm entirely.
        """
        tsquery = build_or_tsquery("What did guests say about finding product-market fit?")
        assert "|" in tsquery
        assert "&" not in tsquery
        assert "'product-market'" in tsquery
        assert "'fit'" in tsquery

    def test_drops_question_scaffolding(self):
        tsquery = build_or_tsquery("What did guests say about pricing?")
        assert "'guests'" not in tsquery
        assert "'what'" not in tsquery
        assert "'pricing'" in tsquery

    def test_keeps_domain_acronyms_despite_length(self):
        tsquery = build_or_tsquery("How do you measure PMF for B2B?")
        assert "'pmf'" in tsquery
        assert "'b2b'" in tsquery

    def test_escapes_tsquery_operators(self):
        """User input reaches to_tsquery, which has its own syntax."""
        tsquery = build_or_tsquery("pricing & growth | (strategy) !important :*")
        for operator in ["&", "(", ")", "!", ":", "*"]:
            assert operator not in tsquery.replace("|", "")
        assert "'pricing'" in tsquery

    def test_empty_and_stopword_only_queries(self):
        assert build_or_tsquery("") == ""
        assert build_or_tsquery("what is the") == ""

    def test_deduplicates_terms(self):
        assert build_or_tsquery("growth growth growth").count("'growth'") == 1


class TestFusion:
    def test_rrf_rewards_agreement_between_arms(self):
        """A document both arms rank highly must beat one only a single arm found."""
        lexical = [10, 20, 30]
        dense = [DenseHit(chunk_id=20, score=0.9), DenseHit(chunk_id=40, score=0.8)]

        fused = dict(HybridRetriever._fuse(lexical, dense))

        assert fused[20] > fused[10], "agreed-on doc should outrank lexical-only top hit"
        assert fused[20] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
        assert fused[10] == pytest.approx(1 / (RRF_K + 1))

    def test_handles_a_single_empty_arm(self):
        fused = dict(HybridRetriever._fuse([], [DenseHit(chunk_id=7, score=0.5)]))
        assert fused == {7: pytest.approx(1 / (RRF_K + 1))}

    def test_handles_both_arms_empty(self):
        assert HybridRetriever._fuse([], []) == []


def _chunk(chunk_id: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        episode=EpisodeRef(slug="ep", title="Episode", guest="Guest"),
        content="content",
    )


class TestGroundingDecision:
    """The 'I don't have enough information' gate."""

    def test_declines_when_nothing_retrieved(self):
        assert RetrievalResult().is_well_grounded(0.55) is False

    def test_declines_on_low_similarity(self):
        """Off-topic queries measured 0.42-0.45 against the real corpus."""
        result = RetrievalResult(chunks=[_chunk()], top_similarity=0.45)
        assert result.is_well_grounded(0.55) is False

    def test_answers_on_high_similarity(self):
        """On-topic queries measured 0.65-0.72 against the real corpus."""
        result = RetrievalResult(chunks=[_chunk()], top_similarity=0.70)
        assert result.is_well_grounded(0.55) is True

    def test_exact_term_match_overrides_mediocre_similarity(self):
        """A passage containing every query term is evidence in its own right."""
        result = RetrievalResult(
            chunks=[_chunk()], top_similarity=0.30, strong_lexical_match=True
        )
        assert result.is_well_grounded(0.55) is True

    def test_disjunctive_hit_count_is_not_a_confidence_signal(self):
        """REGRESSION: `lexical_count` is ~40 even for nonsense queries.

        "sourdough bread" matched 40 passages on the words "best" and
        "starter". Only `strong_lexical_match` (conjunctive) may gate an answer.
        """
        result = RetrievalResult(
            chunks=[_chunk()],
            lexical_count=40,
            top_similarity=0.45,
            strong_lexical_match=False,
        )
        assert result.is_well_grounded(0.55) is False


class TestDenseIndex:
    def test_empty_index_returns_nothing(self):
        index = DenseIndex()
        assert index.is_ready is False
        assert index.search([0.1] * 8, limit=5) == []

    def test_search_ranks_by_cosine_similarity(self):
        index = DenseIndex()
        # Populate directly: loading from Postgres is covered by integration tests.
        index._matrix = np.array(  # noqa: SLF001
            [[1.0, 0.0], [0.0, 1.0], [0.7071, 0.7071]], dtype=np.float32
        )
        index._chunk_ids = np.array([100, 200, 300], dtype=np.int64)  # noqa: SLF001
        index._dimensions = 2  # noqa: SLF001

        hits = index.search([1.0, 0.0], limit=3)

        assert [hit.chunk_id for hit in hits] == [100, 300, 200]
        assert hits[0].score == pytest.approx(1.0, abs=1e-4)

    def test_rejects_mismatched_query_dimensions(self):
        """A changed OLLAMA_EMBED_MODEL must degrade, not crash or return junk."""
        index = DenseIndex()
        index._matrix = np.array([[1.0, 0.0]], dtype=np.float32)  # noqa: SLF001
        index._chunk_ids = np.array([1], dtype=np.int64)  # noqa: SLF001
        index._dimensions = 2  # noqa: SLF001

        assert index.search([1.0, 0.0, 0.0], limit=1) == []

    def test_zero_vector_does_not_produce_nan(self):
        index = DenseIndex()
        index._matrix = np.array([[1.0, 0.0]], dtype=np.float32)  # noqa: SLF001
        index._chunk_ids = np.array([1], dtype=np.int64)  # noqa: SLF001
        index._dimensions = 2  # noqa: SLF001

        assert index.search([0.0, 0.0], limit=1) == []


class TestDeepLinks:
    def test_builds_a_timestamped_youtube_link(self):
        chunk = RetrievedChunk(
            chunk_id=1,
            episode=EpisodeRef(
                slug="brian-chesky",
                title="Brian Chesky's new playbook",
                youtube_url="https://www.youtube.com/watch?v=4ef0juAMqoE",
            ),
            content="...",
            start_seconds=2340,
        )
        assert chunk.deep_link == "https://www.youtube.com/watch?v=4ef0juAMqoE&t=2340s"

    def test_falls_back_when_timestamp_is_missing(self):
        """Timestamp-free episodes still cite, just without the seek."""
        chunk = RetrievedChunk(
            chunk_id=1,
            episode=EpisodeRef(
                slug="x", title="T", youtube_url="https://www.youtube.com/watch?v=abc"
            ),
            content="...",
            start_seconds=None,
        )
        assert chunk.deep_link == "https://www.youtube.com/watch?v=abc"

    def test_no_url_means_no_link(self):
        chunk = RetrievedChunk(
            chunk_id=1, episode=EpisodeRef(slug="x", title="T"), content="...", start_seconds=10
        )
        assert chunk.deep_link is None
