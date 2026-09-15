"""Skill framework.

A *skill* is one bounded capability: a defined input, its own prompt, its own
output validation, and a declared need for retrieval. The router picks exactly
one per turn.

This is deliberately not "one big prompt that does everything". Separate skills
mean the essay's length check cannot leak into Q&A, an artifact failure cannot
corrupt a chat answer, and each capability can be tested in isolation. The
boundary is the point.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import Settings
from app.domain import ArtifactKind, Citation, Message, SkillName
from app.providers.base import LLMProvider
from app.retrieval.hybrid import HybridRetriever, RetrievalResult

PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """Read a prompt from ``skills/prompts/``.

    Prompts live in versioned files rather than string literals so they can be
    reviewed as prose, diffed meaningfully, and edited without touching Python.
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt '{name}' not found at {path}")
    return path.read_text(encoding="utf-8").strip()


@dataclass(slots=True)
class ArtifactDraft:
    """An artifact a skill produced, before safety processing and storage."""

    kind: ArtifactKind
    title: str
    content: str


@dataclass(slots=True)
class SkillContext:
    """Everything a skill needs to do its job."""

    question: str
    history: list[Message]
    provider: LLMProvider
    retriever: HybridRetriever
    settings: Settings
    #: Set by the router when it detected an explicit artifact request.
    requested_artifact_kind: ArtifactKind | None = None


@dataclass(slots=True)
class SkillResult:
    """What a skill produces."""

    content: str
    citations: list[Citation] = field(default_factory=list)
    artifact: ArtifactDraft | None = None
    #: Diagnostics stored on the message and surfaced in logs: retrieval counts,
    #: validation outcomes, refusal reasons, word counts.
    metadata: dict[str, Any] = field(default_factory=dict)
    #: True when the skill declined for lack of grounding. Not an error — a
    #: correct outcome — but tracked separately so it is visible in metrics.
    declined: bool = False


class Skill(abc.ABC):
    """One capability of the assistant."""

    name: SkillName
    #: Whether the skill needs transcript retrieval before it can run.
    requires_retrieval: bool = True

    @abc.abstractmethod
    async def run(self, context: SkillContext, retrieval: RetrievalResult) -> SkillResult:
        """Execute the skill against the retrieved evidence."""


def format_history(history: list[Message], *, max_turns: int = 6) -> str:
    """Render recent conversation for prompt context.

    Truncated per message and per conversation: a local 3B model has a small
    effective context, and letting history crowd out the retrieved passages is
    the fastest way to make answers *less* grounded.
    """
    if not history:
        return ""
    recent = history[-max_turns:]
    lines = []
    for message in recent:
        role = "User" if message.role.value == "user" else "Assistant"
        text = " ".join(message.content.split())
        if len(text) > 600:
            text = text[:600] + "…"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)
