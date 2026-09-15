"""Agent behaviour: routing, citation validation, skills, provider failure."""

from __future__ import annotations

import pytest

from app.agent.query import build_retrieval_query, is_follow_up
from app.agent.router import route, route_by_rules
from app.config import Settings
from app.domain import (
    ArtifactKind,
    Citation,
    EpisodeRef,
    Message,
    MessageRole,
    RetrievedChunk,
    SkillName,
)
from app.errors import ProviderTimeoutError, ProviderUnavailableError
from app.providers.registry import FallbackLLMProvider
from app.retrieval.citations import build_citations, format_context, validate_citations
from app.retrieval.hybrid import RetrievalResult
from app.skills.base import SkillContext
from app.skills.grounded_qa import GroundedQASkill
from app.skills.ship30_essay import evaluate_essay
from tests.conftest import FailingLLM, FakeLLM


class TestRouting:
    @pytest.mark.parametrize(
        "question",
        [
            "Write a Ship 30 for 30 essay about growth loops",
            "write me a 1250 word essay on pricing",
            "Draft a blog post about product-market fit",
            "Create an atomic essay on retention",
        ],
    )
    def test_routes_essay_requests(self, question: str):
        decision = route_by_rules(question)
        assert decision is not None
        assert decision.skill is SkillName.SHIP30_ESSAY

    @pytest.mark.parametrize(
        "question",
        [
            "Create an HTML page summarising this",
            "Build me a landing page about growth",
            "Generate a styled one-pager with CSS",
        ],
    )
    def test_routes_html_artifact_requests(self, question: str):
        decision = route_by_rules(question)
        assert decision is not None
        assert decision.skill is SkillName.ARTIFACT_GEN
        assert decision.artifact_kind is ArtifactKind.HTML

    @pytest.mark.parametrize(
        "question",
        [
            "Create a markdown document from this conversation",
            "Generate a checklist of the tactics we discussed",
            "Make a briefing doc about pricing",
        ],
    )
    def test_routes_markdown_artifact_requests(self, question: str):
        decision = route_by_rules(question)
        assert decision is not None
        assert decision.skill is SkillName.ARTIFACT_GEN
        assert decision.artifact_kind is ArtifactKind.MARKDOWN

    @pytest.mark.parametrize(
        "question",
        [
            "What did guests say about product-market fit?",
            "How do great PMs prioritise?",
            "Why do startups fail at retention?",
        ],
    )
    def test_routes_questions_to_grounded_qa(self, question: str):
        decision = route_by_rules(question)
        assert decision is not None
        assert decision.skill is SkillName.GROUNDED_QA

    def test_a_question_about_building_is_not_a_build_request(self):
        """'How do you build a growth loop?' asks for an answer, not an artifact."""
        decision = route_by_rules("How do you build a growth loop?")
        assert decision is not None
        assert decision.skill is SkillName.GROUNDED_QA

    def test_records_why_it_decided(self):
        decision = route_by_rules("Write a Ship 30 for 30 essay about onboarding")
        assert decision is not None
        assert decision.reason
        assert decision.method == "rule"
        assert decision.matched

    async def test_ambiguous_input_falls_back_to_the_model(self):
        provider = FakeLLM(replies=["ARTIFACT_HTML"])
        decision = await route("growth stuff", provider)
        assert decision.skill is SkillName.ARTIFACT_GEN
        assert decision.method == "model"

    async def test_unparseable_model_output_defaults_to_qa(self):
        """Small models return prose instead of a label. Must not break routing."""
        provider = FakeLLM(replies=["Well, I think this is probably about growth!"])
        decision = await route("growth stuff", provider)
        assert decision.skill is SkillName.GROUNDED_QA
        assert decision.method == "default"

    async def test_router_survives_a_dead_model(self):
        """Routing must never be the reason a request fails."""
        provider = FailingLLM(ProviderUnavailableError("down"))
        decision = await route("ambiguous input here", provider)
        assert decision.skill is SkillName.GROUNDED_QA


def _chunks(count: int = 3) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=index,
            episode=EpisodeRef(
                slug=f"guest-{index}",
                title=f"Episode {index}",
                guest=f"Guest {index}",
                youtube_url=f"https://www.youtube.com/watch?v=vid{index}",
            ),
            content=f"Passage number {index} about growth and retention strategy.",
            speaker=f"Guest {index}",
            start_seconds=index * 100,
            score=0.5,
        )
        for index in range(1, count + 1)
    ]


class TestCitations:
    def test_markers_map_to_retrieved_chunks(self):
        citations = build_citations(_chunks(3))
        assert [c.marker for c in citations] == ["S1", "S2", "S3"]
        assert citations[0].deep_link == "https://www.youtube.com/watch?v=vid1&t=100s"

    def test_strips_fabricated_markers(self):
        """The model invented [S9]; it must not reach the user."""
        citations = build_citations(_chunks(2))
        cleaned, report = validate_citations(
            "Founders should focus [S1]. Also this is true [S9].", citations
        )
        assert "[S9]" not in cleaned
        assert "[S1]" in cleaned
        assert report.fabricated_markers == ["S9"]
        assert [c.marker for c in report.used] == ["S1"]

    def test_reports_only_citations_actually_used(self):
        citations = build_citations(_chunks(3))
        _, report = validate_citations("Only the second source matters [S2].", citations)
        assert [c.marker for c in report.used] == ["S2"]
        assert set(report.unused_markers) == {"S1", "S3"}

    def test_handles_grouped_marker_syntax(self):
        """Models emit '[S1, S3]' despite instructions."""
        citations = build_citations(_chunks(3))
        cleaned, report = validate_citations("Both agree [S1, S3].", citations)
        assert "[S1][S3]" in cleaned
        assert {c.marker for c in report.used} == {"S1", "S3"}

    def test_tidies_punctuation_after_removing_a_marker(self):
        citations = build_citations(_chunks(1))
        cleaned, _ = validate_citations("A claim [S5] .", citations)
        assert "  " not in cleaned
        assert cleaned.endswith(".")

    def test_context_block_labels_passages_as_data(self):
        """Prompt-injection defence: transcript text is never framed as instructions."""
        chunks = _chunks(2)
        block = format_context(chunks, build_citations(chunks))
        assert "[S1]" in block and "[S2]" in block
        assert "Episode:" in block and "Passage:" in block


class TestGroundedQASkill:
    async def test_declines_without_evidence_and_never_calls_the_model(self, settings: Settings):
        """The decisive anti-hallucination measure: don't ask at all."""
        provider = FakeLLM()
        skill = GroundedQASkill()
        context = SkillContext(
            question="What is the airspeed velocity of an unladen swallow?",
            history=[],
            provider=provider,
            retriever=None,  # type: ignore[arg-type]
            settings=settings,
        )
        result = await skill.run(context, RetrievalResult(chunks=_chunks(2), top_similarity=0.20))

        assert result.declined is True
        assert result.citations == []
        assert "don't have enough" in result.content.lower()
        assert provider.calls == [], "model must not be called without evidence"

    async def test_answers_and_validates_citations_when_grounded(self, settings: Settings):
        provider = FakeLLM(replies=["Focus on a narrow segment [S1]. Not this one [S8]."])
        skill = GroundedQASkill()
        context = SkillContext(
            question="How do I find product-market fit?",
            history=[],
            provider=provider,
            retriever=None,  # type: ignore[arg-type]
            settings=settings,
        )
        result = await skill.run(context, RetrievalResult(chunks=_chunks(3), top_similarity=0.72))

        assert result.declined is False
        assert "[S8]" not in result.content, "fabricated marker must be stripped"
        assert result.metadata["citations_fabricated"] == 1
        assert result.metadata["citations_used_inline"] == 1
        # Sources returned are the full evidence set the answer was generated
        # from, so the UI can show provenance even when a small model forgets
        # to mark up every claim.
        assert [c.marker for c in result.citations] == ["S1", "S2", "S3"]
        assert len(provider.calls) == 1


class TestEssayValidator:
    def _essay(self, body_words: int) -> str:
        paragraph = " ".join(f"word{n}" for n in range(body_words))
        return (
            "# A Specific Headline That Promises Something\n\n"
            "Short opener.\n\n"
            f"{paragraph}\n\n"
            "## First Lesson\n\n- point one [S1]\n- point two [S2]\n- point three [S3]\n\n"
            "**This is the point that matters.**\n\n"
            "## Second Lesson\n\nShort line.\n\n"
            "## The takeaway\n\n**Do this one thing** this week.\n"
        )

    def test_flags_a_short_essay(self):
        report = evaluate_essay(self._essay(200))
        assert report.passed is False
        assert any("too short" in failure for failure in report.failures)

    def test_accepts_a_compliant_essay(self):
        report = evaluate_essay(self._essay(1200))
        assert report.word_count >= 1150
        assert report.has_headline and report.has_takeaway
        assert report.citation_count >= 3
        assert report.passed is True, report.failures

    def test_flags_missing_structure(self):
        report = evaluate_essay("Just a wall of prose with no structure at all. " * 120)
        assert report.passed is False
        assert any("headline" in failure for failure in report.failures)
        assert any("bullet" in failure or "heading" in failure for failure in report.failures)

    def test_excludes_markdown_syntax_from_the_word_count(self):
        """Counting '#', '-' and '**' as words lets a short essay pass."""
        with_syntax = "# Title\n\n" + "\n".join(f"- **bold{n}** item" for n in range(50))
        report = evaluate_essay(with_syntax)
        assert report.word_count < 160


class TestProviderFallback:
    async def test_falls_back_when_the_primary_is_unavailable(self):
        primary = FailingLLM(ProviderUnavailableError("ollama down"))
        secondary = FakeLLM(replies=["served by the fallback"])
        provider = FallbackLLMProvider(primary, secondary)

        response = await provider.generate([])

        assert response.text == "served by the fallback"
        assert response.provider == "fake", "response must record who actually served it"

    async def test_falls_back_on_timeout(self):
        provider = FallbackLLMProvider(
            FailingLLM(ProviderTimeoutError("too slow")), FakeLLM(replies=["ok"])
        )
        assert (await provider.generate([])).text == "ok"

    async def test_propagates_when_the_fallback_also_fails(self):
        provider = FallbackLLMProvider(
            FailingLLM(ProviderUnavailableError("down")),
            FailingLLM(ProviderUnavailableError("also down")),
        )
        with pytest.raises(ProviderUnavailableError):
            await provider.generate([])


class TestCitationModel:
    def test_citation_requires_a_real_chunk_id(self):
        """Citations are constructed from retrieval output, never free-form."""
        citation = Citation(marker="S1", chunk_id=42, episode_slug="x", title="T")
        assert citation.chunk_id == 42


class TestFollowUpQueryRewriting:
    """REGRESSION: retrieval lost the topic on follow-up questions.

    "What did guests say about product-market fit?" followed by "What about for
    B2B specifically?" searched the corpus for "B2B specifically" alone and
    returned passages about LinkedIn ads — an answer grounded in the wrong
    subject, which is worse than no answer.
    """

    def _history(self, *questions: str) -> list[Message]:
        from datetime import datetime, timezone
        from uuid import uuid4

        return [
            Message(
                id=uuid4(),
                session_id=uuid4(),
                role=MessageRole.USER,
                content=question,
                created_at=datetime.now(timezone.utc),
            )
            for question in questions
        ]

    @pytest.mark.parametrize(
        "question",
        [
            "What about for B2B specifically?",
            "How about enterprise?",
            "And why does that matter?",
            "Tell me more",
            "Can you elaborate?",
        ],
    )
    def test_detects_follow_ups(self, question: str):
        assert is_follow_up(question) is True

    @pytest.mark.parametrize(
        "question",
        [
            "What did guests say about finding product-market fit?",
            "How do great product managers prioritise their roadmap?",
            "Write a Ship 30 for 30 essay about growth loops",
        ],
    )
    def test_leaves_self_contained_questions_alone(self, question: str):
        """Splicing an unrelated topic into a complete question degrades search."""
        assert is_follow_up(question) is False
        history = self._history("Something entirely unrelated about pricing")
        assert build_retrieval_query(question, history) == question

    def test_splices_the_previous_topic_into_a_follow_up(self):
        history = self._history("What did guests say about finding product-market fit?")
        rewritten = build_retrieval_query("What about for B2B specifically?", history)

        assert "product-market" in rewritten, "the topic must survive into the search"
        assert "B2B" in rewritten, "the qualifier must survive too"
        assert rewritten != "What about for B2B specifically?"

    def test_drops_question_scaffolding_from_the_spliced_topic(self):
        history = self._history("What did guests say about pricing strategy?")
        rewritten = build_retrieval_query("What about for startups?", history)

        assert "pricing" in rewritten and "strategy" in rewritten
        assert not rewritten.lower().startswith("what did")

    def test_no_history_is_a_no_op(self):
        assert build_retrieval_query("What about for B2B?", []) == "What about for B2B?"

    def test_uses_the_most_recent_user_question(self):
        history = self._history("Tell me about hiring", "Tell me about pricing")
        rewritten = build_retrieval_query("What about for startups?", history)
        assert "pricing" in rewritten
        assert "hiring" not in rewritten
