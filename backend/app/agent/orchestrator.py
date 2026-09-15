"""Agent orchestration.

One turn, end to end::

    user message
      -> persist it
      -> route to a skill
      -> retrieve evidence (if the skill needs it)
      -> run the skill
      -> validate output + citations
      -> persist the answer and any artifact
      -> return

Everything that can fail is caught and turned into a typed error with a useful
message. The orchestrator owns the transaction boundaries and the observability
for the turn; the skills stay pure functions of (context, evidence).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

from app.agent.query import build_retrieval_query
from app.agent.router import RouteDecision, route
from app.config import Settings
from app.db.repositories import ArtifactRepository, SessionRepository
from app.domain import Artifact, Citation, Message, MessageRole, SkillName
from app.errors import AppError, ProviderError, SkillExecutionError
from app.observability import get_logger, log_duration, session_id_var
from app.providers.registry import ProviderRegistry
from app.retrieval.hybrid import HybridRetriever, RetrievalResult
from app.skills.artifact_gen import ArtifactGenSkill
from app.skills.base import Skill, SkillContext, SkillResult
from app.skills.grounded_qa import GroundedQASkill
from app.skills.ship30_essay import Ship30EssaySkill

log = get_logger(__name__)

#: Retrieval breadth per skill. An essay needs more source material than a
#: chat answer, or it repeats the same two passages for 1,250 words.
_TOP_K_BY_SKILL: dict[SkillName, int | None] = {
    SkillName.GROUNDED_QA: None,      # use the configured default
    SkillName.SHIP30_ESSAY: 14,
    SkillName.ARTIFACT_GEN: 10,
}


@dataclass(slots=True)
class TurnResult:
    """Everything produced by one exchange."""

    user_message: Message
    assistant_message: Message
    artifact: Artifact | None
    route: RouteDecision
    citations: list[Citation]
    latency_ms: int
    declined: bool


class Agent:
    """Routes a turn to a skill and persists the outcome."""

    def __init__(
        self,
        *,
        settings: Settings,
        sessions: SessionRepository,
        artifacts: ArtifactRepository,
        retriever: HybridRetriever,
        providers: ProviderRegistry,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._artifacts = artifacts
        self._retriever = retriever
        self._providers = providers
        self._skills: dict[SkillName, Skill] = {
            SkillName.GROUNDED_QA: GroundedQASkill(),
            SkillName.SHIP30_ESSAY: Ship30EssaySkill(),
            SkillName.ARTIFACT_GEN: ArtifactGenSkill(),
        }

    async def handle_turn(
        self,
        *,
        session_id: UUID,
        question: str,
        provider_override: str | None = None,
    ) -> TurnResult:
        session_id_var.set(str(session_id))
        started = time.perf_counter()

        provider = self._providers.get_llm(provider_override)

        # History is read BEFORE the new message is stored, so the skill sees
        # the prior conversation without its own question duplicated in it.
        history = await self._sessions.get_recent_messages(session_id, limit=10)

        user_message = await self._sessions.add_message(
            session_id=session_id,
            role=MessageRole.USER,
            content=question,
            set_title_if_new=True,
        )

        decision = await route(question, provider)
        skill = self._skills[decision.skill]

        retrieval = RetrievalResult(query=question)
        if skill.requires_retrieval:
            # Follow-ups ("What about for B2B?") carry no topic of their own, so
            # the search text is rebuilt from the conversation. Generation still
            # receives the real history — only retrieval is rewritten.
            search_query = build_retrieval_query(question, history)
            if search_query != question:
                log.info(
                    "agent.query_rewritten",
                    original_chars=len(question),
                    rewritten_chars=len(search_query),
                    reason="follow_up_needs_conversation_topic",
                )
            retrieval = await self._retriever.search(
                search_query, limit=_TOP_K_BY_SKILL.get(decision.skill)
            )

        context = SkillContext(
            question=question,
            history=history,
            provider=provider,
            retriever=self._retriever,
            settings=self._settings,
            requested_artifact_kind=decision.artifact_kind,
        )

        try:
            with log_duration(
                log,
                "agent.skill_run",
                skill=decision.skill.value,
                provider=provider.name,
                model=provider.model,
            ) as span:
                result: SkillResult = await skill.run(context, retrieval)
                span["declined"] = result.declined
                span["citations"] = len(result.citations)
        except ProviderError:
            raise  # already typed and user-presentable
        except AppError:
            raise
        except Exception as exc:
            log.error(
                "agent.skill_failed",
                skill=decision.skill.value,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise SkillExecutionError(
                detail=f"{decision.skill.value} raised {type(exc).__name__}: {exc}"
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        assistant_message = await self._sessions.add_message(
            session_id=session_id,
            role=MessageRole.ASSISTANT,
            content=result.content,
            skill=decision.skill,
            router_reason=decision.reason,
            model_provider=provider.name,
            model_name=provider.model,
            latency_ms=latency_ms,
            citations=result.citations,
            metadata={
                **result.metadata,
                "route_method": decision.method,
                "route_matched": decision.matched,
                "declined": result.declined,
            },
        )

        artifact: Artifact | None = None
        if result.artifact is not None:
            artifact = await self._artifacts.create(
                session_id=session_id,
                message_id=assistant_message.id,
                kind=result.artifact.kind,
                title=result.artifact.title,
                content=result.artifact.content,
                safety_report=result.metadata.get("safety", {}),
            )

        log.info(
            "agent.turn_complete",
            skill=decision.skill.value,
            provider=provider.name,
            model=provider.model,
            latency_ms=latency_ms,
            citations=len(result.citations),
            declined=result.declined,
            artifact=artifact.kind.value if artifact else None,
        )

        return TurnResult(
            user_message=user_message,
            assistant_message=assistant_message,
            artifact=artifact,
            route=decision,
            citations=result.citations,
            latency_ms=latency_ms,
            declined=result.declined,
        )
