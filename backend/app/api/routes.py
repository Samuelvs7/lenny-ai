"""HTTP routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text

from app.api.deps import AppState, get_state
from app.api.schemas import (
    ArtifactResponse,
    ChatRequest,
    ChatResponse,
    ComponentHealth,
    CreateSessionRequest,
    HealthResponse,
    KnowledgeBaseStats,
    MessageResponse,
    ModelsResponse,
    ProviderInfo,
    RenameSessionRequest,
    RouteInfo,
    SessionDetailResponse,
    SessionSummaryResponse,
)
from app.db.engine import check_connection
from app.domain import Artifact, Message
from app.errors import NotFoundError
from app.observability import get_logger

log = get_logger(__name__)

APP_VERSION = "1.0.0"

router = APIRouter()
health_router = APIRouter(tags=["health"])


# --- Health ------------------------------------------------------------------


@health_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe.

    Deliberately shallow and dependency-free: it answers "is the process
    serving HTTP?". A container orchestrator restarting the API because
    Postgres blinked would turn a recoverable dependency failure into an
    outage. Dependency state lives at ``/health/deep``.
    """
    return HealthResponse(status="ok", version=APP_VERSION)


@health_router.get("/health/deep", response_model=HealthResponse)
async def health_deep(state: AppState = Depends(get_state)) -> HealthResponse:
    """Readiness probe: checks every dependency and reports each one.

    Always returns HTTP 200 with a per-component breakdown. The point is to be
    *diagnostic* — "Ollama is up but llama3.2 is not pulled" is the answer an
    operator needs, and a bare 503 does not carry it.
    """
    components: list[ComponentHealth] = []
    worst = "ok"

    def degrade(level: str) -> None:
        nonlocal worst
        order = {"ok": 0, "degraded": 1, "down": 2}
        if order[level] > order[worst]:
            worst = level

    if state.startup_error:
        components.append(
            ComponentHealth(name="startup", status="degraded", detail=state.startup_error)
        )
        degrade("degraded")

    # Database
    try:
        await check_connection(state.engine)
        components.append(ComponentHealth(name="database", status="ok"))
    except Exception as exc:
        detail = getattr(exc, "detail", None) or type(exc).__name__
        components.append(ComponentHealth(name="database", status="down", detail=detail))
        degrade("down")

    # Knowledge base
    try:
        async with state.engine.connect() as conn:
            chunks = (await conn.execute(text("SELECT COUNT(*) FROM chunks"))).scalar_one()
            embedded = (
                await conn.execute(
                    text("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL")
                )
            ).scalar_one()
        if chunks == 0:
            components.append(
                ComponentHealth(
                    name="knowledge_base",
                    status="down",
                    detail="No transcripts indexed. Run: python -m app.ingestion",
                )
            )
            degrade("down")
        elif embedded == 0:
            components.append(
                ComponentHealth(
                    name="knowledge_base",
                    status="degraded",
                    detail=f"{chunks} chunks indexed but no embeddings; "
                    "retrieval is lexical-only.",
                )
            )
            degrade("degraded")
        else:
            components.append(
                ComponentHealth(
                    name="knowledge_base",
                    status="ok",
                    detail=f"{chunks} chunks, {embedded} embedded, "
                    f"dense index holds {state.dense_index.size}",
                )
            )
    except Exception as exc:
        components.append(
            ComponentHealth(name="knowledge_base", status="down", detail=type(exc).__name__)
        )
        degrade("down")

    # Model providers
    default_provider = state.settings.model_provider.value
    for probe in await state.providers.health_all():
        if probe.available:
            level = "ok"
        else:
            # Only the *active* provider being down makes the app unhealthy.
            # An unconfigured cloud provider is an expected local-only setup.
            level = "down" if probe.name == default_provider else "degraded"
        components.append(
            ComponentHealth(
                name=f"provider:{probe.name}",
                status=level,
                detail=probe.detail or None,
                latency_ms=probe.latency_ms,
            )
        )
        if probe.name == default_provider and not probe.available:
            degrade("down")

    return HealthResponse(status=worst, version=APP_VERSION, components=components)


# --- Models ------------------------------------------------------------------


@router.get("/models", response_model=ModelsResponse, tags=["models"])
async def list_models(state: AppState = Depends(get_state)) -> ModelsResponse:
    """Providers available to the UI model picker."""
    described = state.providers.describe()
    active = state.settings.model_provider.value
    active_model = next(
        (entry["model"] for entry in described if entry["name"] == active), None
    )
    return ModelsResponse(
        active_provider=active,
        active_model=active_model,
        agent_runner=state.settings.agent_runner.value,
        agent_runner_available=_agent_runner_available(state),
        providers=[ProviderInfo(**entry) for entry in described],
    )


def _agent_runner_available(state: AppState) -> bool:
    """Can the configured agent runner actually run?

    The native runner always can. The Claude Agent SDK runner needs both an
    optional dependency and an API key, so its availability is reported rather
    than assumed — a UI that claims a runner is active when it cannot start is
    worse than one that says nothing.
    """
    if state.settings.agent_runner.value != "claude_agent_sdk":
        return True
    try:
        from app.agent.runners.claude_sdk import describe_runner

        return bool(describe_runner(state.settings)["available"])
    except Exception:
        return False


@router.get("/knowledge-base", response_model=KnowledgeBaseStats, tags=["models"])
async def knowledge_base(state: AppState = Depends(get_state)) -> KnowledgeBaseStats:
    """Index state — what is loaded, how fresh it is, what was cleaned."""
    async with state.engine.connect() as conn:
        episodes = (await conn.execute(text("SELECT COUNT(*) FROM episodes"))).scalar_one()
        chunks = (await conn.execute(text("SELECT COUNT(*) FROM chunks"))).scalar_one()
        embedded = (
            await conn.execute(text("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"))
        ).scalar_one()
        sponsors = (
            await conn.execute(
                text(
                    "SELECT COALESCE(SUM(sponsor_segments_removed),0), "
                    "COALESCE(SUM(sponsor_words_removed),0) FROM episodes"
                )
            )
        ).first()
        last = (
            await conn.execute(
                text(
                    "SELECT finished_at, status FROM ingestion_runs "
                    "ORDER BY started_at DESC LIMIT 1"
                )
            )
        ).first()

    return KnowledgeBaseStats(
        episodes=episodes,
        chunks=chunks,
        chunks_with_embeddings=embedded,
        sponsor_segments_removed=sponsors[0] if sponsors else 0,
        sponsor_words_removed=sponsors[1] if sponsors else 0,
        embedding_model=state.dense_index.model,
        dense_index_size=state.dense_index.size,
        last_ingestion_at=last[0] if last else None,
        last_ingestion_status=last[1] if last else None,
    )


# --- Sessions ----------------------------------------------------------------


@router.post(
    "/sessions",
    response_model=SessionDetailResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["sessions"],
)
async def create_session(
    payload: CreateSessionRequest, state: AppState = Depends(get_state)
) -> SessionDetailResponse:
    """Start a new chat. Each session is an independent context."""
    session = await state.sessions.create(
        title=payload.title or "New chat",
        user_metadata=payload.user_metadata,
        model_provider=state.settings.model_provider.value,
        model_name=state.providers.get_llm().model,
    )
    return SessionDetailResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        model_provider=session.model_provider,
        model_name=session.model_name,
        messages=[],
        artifacts=[],
    )


@router.get("/sessions", response_model=list[SessionSummaryResponse], tags=["sessions"])
async def list_sessions(
    limit: int = 50, state: AppState = Depends(get_state)
) -> list[SessionSummaryResponse]:
    summaries = await state.sessions.list_summaries(limit=min(max(limit, 1), 200))
    return [SessionSummaryResponse(**summary.model_dump()) for summary in summaries]


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse, tags=["sessions"])
async def get_session(
    session_id: UUID, state: AppState = Depends(get_state)
) -> SessionDetailResponse:
    session = await state.sessions.get_detail(session_id)
    artifacts = await state.artifacts.list_for_session(session_id)
    return SessionDetailResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        model_provider=session.model_provider,
        model_name=session.model_name,
        messages=[_to_message(m) for m in session.messages],
        artifacts=[_to_artifact(a) for a in artifacts],
    )


@router.patch(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["sessions"],
)
async def rename_session(
    session_id: UUID, payload: RenameSessionRequest, state: AppState = Depends(get_state)
) -> Response:
    await state.sessions.rename(session_id, payload.title.strip())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["sessions"],
)
async def delete_session(
    session_id: UUID, state: AppState = Depends(get_state)
) -> Response:
    await state.sessions.delete(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Chat --------------------------------------------------------------------


@router.post("/sessions/{session_id}/chat", response_model=ChatResponse, tags=["chat"])
async def chat(
    session_id: UUID, payload: ChatRequest, state: AppState = Depends(get_state)
) -> ChatResponse:
    """Send a message and get a grounded answer.

    404s on an unknown session rather than creating one implicitly: a stale
    browser tab pointing at a deleted conversation should say so, not silently
    start a new history the user cannot find later.
    """
    if not await state.sessions.exists(session_id):
        raise NotFoundError(f"Session {session_id} was not found.")

    result = await state.agent.handle_turn(
        session_id=session_id,
        question=payload.message,
        provider_override=payload.provider,
    )

    return ChatResponse(
        session_id=session_id,
        user_message=_to_message(result.user_message),
        assistant_message=_to_message(result.assistant_message),
        artifact=_to_artifact(result.artifact) if result.artifact else None,
        route=RouteInfo(
            skill=result.route.skill,
            reason=result.route.reason,
            method=result.route.method,
            artifact_kind=result.route.artifact_kind,
        ),
        citations=result.citations,
        latency_ms=result.latency_ms,
        declined=result.declined,
    )


# --- Artifacts ---------------------------------------------------------------


@router.get(
    "/sessions/{session_id}/artifacts",
    response_model=list[ArtifactResponse],
    tags=["artifacts"],
)
async def list_artifacts(
    session_id: UUID, state: AppState = Depends(get_state)
) -> list[ArtifactResponse]:
    artifacts = await state.artifacts.list_for_session(session_id)
    return [_to_artifact(a) for a in artifacts]


@router.get("/artifacts/{artifact_id}", response_model=ArtifactResponse, tags=["artifacts"])
async def get_artifact(
    artifact_id: UUID, state: AppState = Depends(get_state)
) -> ArtifactResponse:
    return _to_artifact(await state.artifacts.get(artifact_id))


# --- mappers -----------------------------------------------------------------


def _to_message(message: Message) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        role=message.role,
        content=message.content,
        skill=message.skill,
        router_reason=message.router_reason,
        model_provider=message.model_provider,
        model_name=message.model_name,
        latency_ms=message.latency_ms,
        citations=message.citations,
        metadata=message.metadata,
        created_at=message.created_at,
    )


def _to_artifact(artifact: Artifact) -> ArtifactResponse:
    return ArtifactResponse(
        id=artifact.id,
        session_id=artifact.session_id,
        message_id=artifact.message_id,
        kind=artifact.kind,
        title=artifact.title,
        content=artifact.content,
        version=artifact.version,
        safety_report=artifact.safety_report,
        created_at=artifact.created_at,
    )
