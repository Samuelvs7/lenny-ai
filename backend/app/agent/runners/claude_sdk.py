"""Claude Agent SDK runner.

## Why this exists alongside the native runner

The brief requires two things that pull in opposite directions:

* *"Build the agent layer using the Anthropic Claude Agent SDK or Pi Coding
  Agent."*
* *"Local LLM — mandatory for the demo: run the submitted demo using Ollama."*

The Claude Agent SDK is Claude Code packaged as a library. It runs against
Anthropic's models; it does not drive Ollama. So a single SDK-only agent layer
cannot satisfy the local-demo requirement, and a single Ollama-only layer
cannot satisfy the SDK requirement.

Rather than fake either, the **skills are defined once** and two thin executors
run them:

* :class:`~app.agent.orchestrator.Agent` — the native runner. Provider-agnostic,
  drives the mandated Ollama demo, and is the default.
* This module — the same retrieval and the same grounding rules exposed to the
  official SDK as in-process MCP tools, so the SDK's agent loop performs the
  work when a cloud key is available.

Both share `retrieval/`, `skills/prompts/`, and the citation validator. The
difference is only *who runs the loop*.

## Status — read this before relying on it

The native runner is verified end to end against a live local model. **This
runner is not**: no `ANTHROPIC_API_KEY` was available on the build machine, so
it has been written against the documented SDK API and exercised only through
its import guard and tool-definition tests. It is wired, not proven. That
distinction is recorded here and in `agent-transcripts/03-verification-log.md`
rather than glossed over.

## Requirements

    pip install -r requirements-agent-sdk.txt

The SDK is deliberately **not** in `requirements.txt`: it is a ~99 MB wheel
bundling the Claude Code CLI, and the default local path does not need it.
"""

from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.errors import ProviderUnavailableError, SkillExecutionError
from app.observability import get_logger
from app.retrieval.citations import build_citations, format_context
from app.retrieval.hybrid import HybridRetriever
from app.skills.base import load_prompt

log = get_logger(__name__)

#: Ceiling on agent turns, so a loop cannot run away and bill indefinitely.
MAX_AGENT_TURNS = 6


def _require_sdk() -> Any:
    """Import the SDK, or fail with an actionable message.

    Imported lazily so the application starts, and every other code path works,
    without the SDK installed.
    """
    try:
        import claude_agent_sdk
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ProviderUnavailableError(
            message="The Claude Agent SDK is not installed.",
            detail=(
                "AGENT_RUNNER=claude_agent_sdk requires it. Install with "
                "`pip install -r requirements-agent-sdk.txt`, or set "
                "AGENT_RUNNER=native to use the provider-agnostic runner."
            ),
        ) from exc
    return claude_agent_sdk


class ClaudeAgentSDKRunner:
    """Runs a turn through Anthropic's Claude Agent SDK.

    Retrieval is exposed to the agent as a tool rather than pre-injected into
    the prompt, which is the whole point of using an agent loop here: the model
    decides what to search for, can search again after reading results, and can
    refine a query for a follow-up. The native runner retrieves once, up front.
    """

    def __init__(self, *, settings: Settings, retriever: HybridRetriever) -> None:
        self._settings = settings
        self._retriever = retriever

        if not settings.is_anthropic_configured:
            raise ProviderUnavailableError(
                message="The Claude Agent SDK runner needs an Anthropic API key.",
                detail=(
                    "Set ANTHROPIC_API_KEY, or set AGENT_RUNNER=native to run "
                    "locally on Ollama."
                ),
            )

    # --- tools ---------------------------------------------------------------

    def _build_tools(self) -> list[Any]:
        """Expose transcript search to the agent as an MCP tool.

        The tool returns passages already labelled with the ``[S#]`` markers the
        model must cite, so the SDK path produces citations in exactly the same
        format the validator understands. Grounding rules are shared, not
        reimplemented.
        """
        sdk = _require_sdk()
        retriever = self._retriever
        min_similarity = self._settings.retrieval_min_similarity

        @sdk.tool(
            "search_transcripts",
            "Search Lenny's Podcast transcripts for passages relevant to a query. "
            "Returns labelled passages ([S1], [S2], …) that must be cited in the "
            "answer. Call this before answering any question about the podcast.",
            {"query": str},
        )
        async def search_transcripts(args: dict[str, Any]) -> dict[str, Any]:
            query = (args.get("query") or "").strip()
            if not query:
                return {
                    "content": [
                        {"type": "text", "text": "No query supplied — nothing to search."}
                    ]
                }

            result = await retriever.search(query)

            # The same grounding gate as the native runner. Reporting "nothing
            # relevant" to the agent is what lets it decline honestly rather
            # than answering from its own parametric knowledge.
            if not result.is_well_grounded(min_similarity):
                log.info(
                    "agent_sdk.search_insufficient",
                    query_chars=len(query),
                    top_similarity=round(result.top_similarity, 4),
                )
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "No sufficiently relevant passages found "
                                f"(best similarity {result.top_similarity:.2f}, "
                                f"threshold {min_similarity}). Do not answer from "
                                "general knowledge — tell the user the indexed "
                                "transcripts do not cover this."
                            ),
                        }
                    ]
                }

            citations = build_citations(result.chunks)
            log.info(
                "agent_sdk.search_ok",
                query_chars=len(query),
                passages=len(citations),
                top_similarity=round(result.top_similarity, 4),
            )
            return {
                "content": [
                    {"type": "text", "text": format_context(result.chunks, citations)}
                ],
                # Structured copy so the caller can rebuild citation objects
                # after the run without re-parsing the prose.
                "metadata": {
                    "citations": [c.model_dump(mode="json") for c in citations]
                },
            }

        return [search_transcripts]

    # --- execution -----------------------------------------------------------

    async def run(self, question: str, *, history_summary: str = "") -> dict[str, Any]:
        """Execute one turn. Returns the answer text plus diagnostics."""
        sdk = _require_sdk()

        server = sdk.create_sdk_mcp_server(
            name="lenny-transcripts",
            version="1.0.0",
            tools=self._build_tools(),
        )

        system_prompt = load_prompt("grounded_qa")
        if history_summary:
            system_prompt = f"{system_prompt}\n\n## Conversation so far\n{history_summary}"

        options = sdk.ClaudeAgentOptions(
            model=self._settings.anthropic_model,
            system_prompt=system_prompt,
            mcp_servers={"lenny": server},
            # Only our retrieval tool. The SDK's built-in file and bash tools are
            # deliberately withheld: this agent answers questions about a corpus,
            # and giving it filesystem or shell access would be a needless and
            # significant expansion of what a prompt injection could reach.
            allowed_tools=["mcp__lenny__search_transcripts"],
            max_turns=MAX_AGENT_TURNS,
        )

        chunks: list[str] = []
        try:
            async for message in sdk.query(prompt=question, options=options):
                for block in getattr(message, "content", None) or []:
                    if getattr(block, "type", None) == "text":
                        chunks.append(getattr(block, "text", "") or "")
        except Exception as exc:
            log.error("agent_sdk.run_failed", error=f"{type(exc).__name__}: {exc}")
            raise SkillExecutionError(
                message="The Claude Agent SDK run did not complete.",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

        answer = "".join(chunks).strip()
        if not answer:
            raise SkillExecutionError(detail="Claude Agent SDK returned no text output")

        return {
            "answer": answer,
            "runner": "claude_agent_sdk",
            "model": self._settings.anthropic_model,
        }


def describe_runner(settings: Settings) -> dict[str, Any]:
    """Runner availability, for `/api/models` and the status panel."""
    available = True
    reason: str | None = None

    try:
        _require_sdk()
    except ProviderUnavailableError as exc:
        available, reason = False, exc.detail

    if available and not settings.is_anthropic_configured:
        available, reason = False, "ANTHROPIC_API_KEY is not set."

    return {
        "name": "claude_agent_sdk",
        "available": available,
        "reason": reason,
        "active": settings.agent_runner.value == "claude_agent_sdk",
        "notes": json.dumps(
            {
                "tools": ["search_transcripts"],
                "filesystem_access": False,
                "max_turns": MAX_AGENT_TURNS,
            }
        ),
    }
