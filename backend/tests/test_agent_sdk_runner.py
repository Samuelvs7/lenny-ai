"""Claude Agent SDK runner wiring.

No API key was available on the build machine, so these tests verify everything
that can be verified without one: the import guard, the configuration guard, and
that the MCP tool definition actually constructs against the real SDK and
enforces the same grounding gate as the native runner.

A live end-to-end SDK run remains unverified. That is stated in
`agent-transcripts/03-verification-log.md` rather than implied to work.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.domain import EpisodeRef, RetrievedChunk
from app.errors import ProviderUnavailableError
from app.retrieval.hybrid import RetrievalResult

sdk = pytest.importorskip(
    "claude_agent_sdk",
    reason="Install with `pip install -r requirements-agent-sdk.txt` to run these",
)

from app.agent.runners.claude_sdk import (  # noqa: E402
    MAX_AGENT_TURNS,
    ClaudeAgentSDKRunner,
    describe_runner,
)


class FakeRetriever:
    """Stands in for HybridRetriever, returning a scripted result."""

    def __init__(self, result: RetrievalResult) -> None:
        self._result = result
        self.queries: list[str] = []

    async def search(self, query: str, **_kwargs) -> RetrievalResult:
        self.queries.append(query)
        return self._result


def _grounded() -> RetrievalResult:
    return RetrievalResult(
        chunks=[
            RetrievedChunk(
                chunk_id=1,
                episode=EpisodeRef(
                    slug="brian-chesky",
                    title="Brian Chesky's new playbook",
                    guest="Brian Chesky",
                    youtube_url="https://www.youtube.com/watch?v=abc",
                ),
                content="Founders should be in the details of every launch.",
                speaker="Brian Chesky",
                start_seconds=120,
            )
        ],
        top_similarity=0.72,
    )


def _configured() -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test-not-a-real-key",
        anthropic_model="claude-opus-5",
        agent_runner="claude_agent_sdk",
        app_env="test",
        log_level="WARNING",
    )


class TestConfigurationGuards:
    def test_refuses_to_construct_without_a_key(self):
        """Fail at construction with a clear message, not mid-conversation."""
        settings = Settings(anthropic_api_key="", app_env="test", log_level="WARNING")
        with pytest.raises(ProviderUnavailableError) as excinfo:
            ClaudeAgentSDKRunner(settings=settings, retriever=FakeRetriever(_grounded()))
        assert "ANTHROPIC_API_KEY" in (excinfo.value.detail or "")

    def test_describe_reports_availability(self):
        described = describe_runner(_configured())
        assert described["name"] == "claude_agent_sdk"
        assert described["active"] is True

    def test_describe_explains_a_missing_key(self):
        described = describe_runner(
            Settings(anthropic_api_key="", app_env="test", log_level="WARNING")
        )
        assert described["available"] is False
        assert "ANTHROPIC_API_KEY" in (described["reason"] or "")


class TestToolWiring:
    def test_builds_a_real_mcp_server(self):
        """The tool definition must construct against the actual SDK."""
        runner = ClaudeAgentSDKRunner(
            settings=_configured(), retriever=FakeRetriever(_grounded())
        )
        tools = runner._build_tools()  # noqa: SLF001
        assert len(tools) == 1

        server = sdk.create_sdk_mcp_server(
            name="lenny-transcripts", version="1.0.0", tools=tools
        )
        assert server is not None

    async def test_tool_returns_labelled_passages_when_grounded(self):
        retriever = FakeRetriever(_grounded())
        runner = ClaudeAgentSDKRunner(settings=_configured(), retriever=retriever)
        tool = runner._build_tools()[0]  # noqa: SLF001

        result = await tool.handler({"query": "product-market fit"})

        text = result["content"][0]["text"]
        assert "[S1]" in text, "passages must carry the markers the model cites"
        assert "Brian Chesky" in text
        assert retriever.queries == ["product-market fit"]
        assert result["metadata"]["citations"][0]["chunk_id"] == 1

    async def test_tool_enforces_the_same_grounding_gate(self):
        """The SDK path must decline on weak evidence, exactly as native does."""
        weak = RetrievalResult(chunks=_grounded().chunks, top_similarity=0.20)
        runner = ClaudeAgentSDKRunner(settings=_configured(), retriever=FakeRetriever(weak))
        tool = runner._build_tools()[0]  # noqa: SLF001

        text = (await tool.handler({"query": "sourdough bread"}))["content"][0]["text"]

        assert "No sufficiently relevant passages" in text
        assert "Do not answer from general knowledge" in text

    async def test_tool_handles_an_empty_query(self):
        runner = ClaudeAgentSDKRunner(
            settings=_configured(), retriever=FakeRetriever(_grounded())
        )
        tool = runner._build_tools()[0]  # noqa: SLF001
        text = (await tool.handler({"query": "  "}))["content"][0]["text"]
        assert "nothing to search" in text.lower()


class TestSafetyPosture:
    def test_turn_limit_is_bounded(self):
        """An unbounded agent loop can run away and bill indefinitely."""
        assert 1 <= MAX_AGENT_TURNS <= 10

    def test_no_filesystem_or_shell_tools_are_granted(self):
        """The SDK ships Read/Write/Bash. This agent must not get them.

        Withholding them keeps a prompt injection from reaching the filesystem
        or a shell — the agent only needs to search a corpus.
        """
        notes = json.loads(describe_runner(_configured())["notes"])
        assert notes["filesystem_access"] is False
        assert notes["tools"] == ["search_transcripts"]
