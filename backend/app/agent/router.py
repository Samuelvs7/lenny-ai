"""Intent routing.

Picks exactly one skill per turn and explains why. The reason is stored on the
message and returned to the UI, so routing is auditable after the fact instead
of being a black box.

**Why rules first, model second.** The obvious design is "ask the LLM to pick a
skill". That is slow (an extra round trip before any work starts), unreliable on
a 3B local model, and fails in the most annoying way possible — silently routing
a Ship 30 request to plain Q&A. In practice the intents here are lexically
distinctive: people ask for an essay, a document, or a web page in recognisable
language. So:

1. **Deterministic rules** handle the clear cases. Fast, testable, free.
2. **Model classification** is the fallback for genuinely ambiguous input, and
   only when a model is available.
3. **Grounded Q&A** is the default, because it is the safe outcome: answering a
   question when an artifact was wanted is a minor annoyance, while generating
   an artifact when an answer was wanted throws away the user's question.

Every decision records ``matched`` (the signal that fired) so a mis-route can be
diagnosed from one log line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain import ArtifactKind, SkillName
from app.observability import get_logger
from app.providers.base import ChatMessage, LLMProvider, Role

log = get_logger(__name__)


@dataclass(slots=True)
class RouteDecision:
    skill: SkillName
    reason: str
    artifact_kind: ArtifactKind | None = None
    #: 'rule' or 'model' — how the decision was reached.
    method: str = "rule"
    matched: str | None = None


# --- Signals -----------------------------------------------------------------

#: Ship 30 is a named, specific deliverable. Users ask for it by name, or by
#: describing the artefact it produces.
_SHIP30 = re.compile(
    r"\b(ship\s*30(\s*for\s*30)?"
    r"|atomic essay"
    r"|(write|draft|create|generate)\s+(me\s+)?(an?\s+)?(\d[,\d]*\s*word\s+)?"
    r"(essay|blog post|article|newsletter|long-?form post))\b",
    re.IGNORECASE,
)

_HTML_ARTIFACT = re.compile(
    r"\b(html|css|web\s?page|webpage|landing page|one-?pager"
    r"|styled page|rendered page|infographic)\b",
    re.IGNORECASE,
)

_MARKDOWN_ARTIFACT = re.compile(
    r"\b(markdown|md file|\.md\b|document|doc|one-?pager|brief|briefing"
    r"|cheat\s?sheet|checklist|summary document|playbook|template|report)\b",
    re.IGNORECASE,
)

#: A verb indicating the user wants an object produced, not a question answered.
_ARTIFACT_VERB = re.compile(
    r"\b(create|make|generate|build|produce|draft|turn (this|that) into|"
    r"put (this|that) (in)?to|format (this|that) as|render|design)\b",
    re.IGNORECASE,
)

#: Explicit question shapes. Used to stop an artifact verb from hijacking a
#: genuine question ("how do you build a growth loop?" is not a build request).
_QUESTION = re.compile(
    r"(^\s*(what|how|why|when|who|which|where|does|do|did|is|are|can|should|could)\b|\?\s*$)",
    re.IGNORECASE,
)


def route_by_rules(question: str) -> RouteDecision | None:
    """Deterministic routing. Returns ``None`` when the input is ambiguous."""
    text = " ".join(question.split())

    if match := _SHIP30.search(text):
        return RouteDecision(
            skill=SkillName.SHIP30_ESSAY,
            reason="Request names a Ship 30 for 30-style essay deliverable.",
            method="rule",
            matched=match.group(0),
        )

    has_verb = bool(_ARTIFACT_VERB.search(text))
    html_match = _HTML_ARTIFACT.search(text)
    markdown_match = _MARKDOWN_ARTIFACT.search(text)

    if html_match and (has_verb or not _QUESTION.search(text)):
        return RouteDecision(
            skill=SkillName.ARTIFACT_GEN,
            reason="Request asks for a rendered HTML/CSS artifact.",
            artifact_kind=ArtifactKind.HTML,
            method="rule",
            matched=html_match.group(0),
        )

    if markdown_match and has_verb:
        return RouteDecision(
            skill=SkillName.ARTIFACT_GEN,
            reason="Request asks for a written document artifact.",
            artifact_kind=ArtifactKind.MARKDOWN,
            method="rule",
            matched=markdown_match.group(0),
        )

    if _QUESTION.search(text):
        return RouteDecision(
            skill=SkillName.GROUNDED_QA,
            reason="Input is a question; answering from transcripts.",
            method="rule",
            matched="question form",
        )

    return None


_CLASSIFIER_PROMPT = """You are a router. Classify the user's request into exactly one category.

Categories:
- QA: a question to answer from podcast transcripts.
- ESSAY: a request to write a long-form essay or article (~1250 words).
- ARTIFACT_MD: a request to produce a Markdown document, brief, checklist or summary file.
- ARTIFACT_HTML: a request to produce an HTML/CSS page, styled page or web page.

Reply with the category name only. No explanation, no punctuation."""

_VALID_LABELS = {
    "QA": (SkillName.GROUNDED_QA, None),
    "ESSAY": (SkillName.SHIP30_ESSAY, None),
    "ARTIFACT_MD": (SkillName.ARTIFACT_GEN, ArtifactKind.MARKDOWN),
    "ARTIFACT_HTML": (SkillName.ARTIFACT_GEN, ArtifactKind.HTML),
}


async def route_by_model(question: str, provider: LLMProvider) -> RouteDecision | None:
    """Ask the model to classify. Returns ``None`` if it gives an unusable answer."""
    try:
        response = await provider.generate(
            [
                ChatMessage(role=Role.SYSTEM, content=_CLASSIFIER_PROMPT),
                ChatMessage(role=Role.USER, content=question[:1000]),
            ],
            max_tokens=12,
            temperature=0.0,
        )
    except Exception as exc:
        # Routing must never be the reason a request fails: fall through to the
        # default rather than propagating.
        log.warning("router.model_failed", error=type(exc).__name__)
        return None

    # Small models add punctuation, quotes and explanation despite instructions.
    label = re.sub(r"[^A-Z_]", "", response.text.strip().upper())
    for candidate, (skill, kind) in _VALID_LABELS.items():
        if label.startswith(candidate):
            return RouteDecision(
                skill=skill,
                reason=f"Model classified this request as {candidate}.",
                artifact_kind=kind,
                method="model",
                matched=candidate,
            )

    log.warning("router.model_unparseable", raw=response.text[:80])
    return None


async def route(question: str, provider: LLMProvider | None = None) -> RouteDecision:
    """Choose the skill for this turn."""
    decision = route_by_rules(question)

    if decision is None and provider is not None:
        decision = await route_by_model(question, provider)

    if decision is None:
        decision = RouteDecision(
            skill=SkillName.GROUNDED_QA,
            reason="No clear artifact or essay intent; defaulting to grounded Q&A.",
            method="default",
        )

    log.info(
        "router.decision",
        skill=decision.skill.value,
        method=decision.method,
        matched=decision.matched,
        artifact_kind=decision.artifact_kind.value if decision.artifact_kind else None,
    )
    return decision
