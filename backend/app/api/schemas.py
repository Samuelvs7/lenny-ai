"""API request and response contracts.

Explicit models rather than raw dicts: FastAPI validates them, the OpenAPI
schema documents them, and the frontend's types are generated from the same
source of truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.domain import ArtifactKind, Citation, MessageRole, SkillName

#: Bounds on a user turn. The lower bound rejects empty submissions; the upper
#: bound stops a pasted document from blowing out the model's context and the
#: request timeout.
MIN_MESSAGE_CHARS = 1
MAX_MESSAGE_CHARS = 4000


# --- Sessions ----------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    user_metadata: dict[str, Any] = Field(default_factory=dict)


class SessionSummaryResponse(BaseModel):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_message_preview: str | None = None


class MessageResponse(BaseModel):
    id: UUID
    role: MessageRole
    content: str
    skill: SkillName | None = None
    router_reason: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    latency_ms: int | None = None
    citations: list[Citation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ArtifactResponse(BaseModel):
    id: UUID
    session_id: UUID
    message_id: UUID | None = None
    kind: ArtifactKind
    title: str
    content: str
    version: int
    safety_report: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SessionDetailResponse(BaseModel):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    model_provider: str | None = None
    model_name: str | None = None
    messages: list[MessageResponse] = Field(default_factory=list)
    artifacts: list[ArtifactResponse] = Field(default_factory=list)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


# --- Chat --------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(min_length=MIN_MESSAGE_CHARS, max_length=MAX_MESSAGE_CHARS)
    #: Per-request provider override from the UI model picker. Falls back to
    #: MODEL_PROVIDER when absent.
    provider: str | None = Field(default=None, max_length=32)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("message cannot be empty or whitespace only")
        return cleaned


class RouteInfo(BaseModel):
    """Why the agent did what it did — surfaced in the UI, not just the logs."""

    skill: SkillName
    reason: str
    method: str
    artifact_kind: ArtifactKind | None = None


class ChatResponse(BaseModel):
    session_id: UUID
    user_message: MessageResponse
    assistant_message: MessageResponse
    artifact: ArtifactResponse | None = None
    route: RouteInfo
    citations: list[Citation] = Field(default_factory=list)
    latency_ms: int
    #: True when the assistant declined for lack of grounding. The UI renders
    #: this differently from an answer — it is not a failure, but it is not a
    #: normal answer either.
    declined: bool = False


# --- Models / health ---------------------------------------------------------


class ProviderInfo(BaseModel):
    name: str
    available: bool
    model: str | None = None
    is_default: bool = False
    reason: str | None = None


class ModelsResponse(BaseModel):
    active_provider: str
    active_model: str | None = None
    agent_runner: str
    #: False when the configured runner cannot start (missing SDK or API key).
    agent_runner_available: bool = True
    providers: list[ProviderInfo]


class ComponentHealth(BaseModel):
    name: str
    status: str  # ok | degraded | down
    detail: str | None = None
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    status: str  # ok | degraded | down
    version: str
    components: list[ComponentHealth] = Field(default_factory=list)


class KnowledgeBaseStats(BaseModel):
    episodes: int
    chunks: int
    chunks_with_embeddings: int
    sponsor_segments_removed: int
    sponsor_words_removed: int
    embedding_model: str | None = None
    dense_index_size: int = 0
    last_ingestion_at: datetime | None = None
    last_ingestion_status: str | None = None
