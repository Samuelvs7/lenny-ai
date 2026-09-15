"""Domain types shared by the API, the agent layer, and persistence.

These are the vocabulary of the system. Keeping them in one module (rather than
defining near-identical shapes in the router, the repository and the API layer)
means a change to what a citation *is* happens in exactly one place.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class SkillName(str, Enum):
    """The three things this assistant knows how to do.

    Kept closed deliberately: the router must choose one of these, and an
    unknown skill name is a routing bug we want to fail loudly rather than
    silently pass through to a generic prompt.
    """

    GROUNDED_QA = "grounded_qa"
    SHIP30_ESSAY = "ship30_essay"
    ARTIFACT_GEN = "artifact_gen"


class ArtifactKind(str, Enum):
    MARKDOWN = "markdown"
    HTML = "html"


# --- Knowledge base ----------------------------------------------------------


class EpisodeRef(BaseModel):
    """Enough of an episode to render and verify a citation."""

    slug: str
    guest: str | None = None
    title: str
    youtube_url: str | None = None
    publish_date: date | None = None


class RetrievedChunk(BaseModel):
    """A passage returned by retrieval, with its provenance and scores."""

    chunk_id: int
    episode: EpisodeRef
    content: str
    speaker: str | None = None
    start_seconds: int | None = None
    end_seconds: int | None = None

    # Fused relevance, plus the per-arm ranks that produced it. Exposed because
    # "why did it retrieve this?" is the first question when grounding looks
    # wrong, and answering it from logs alone is painful.
    score: float = 0.0
    lexical_rank: int | None = None
    dense_rank: int | None = None

    @property
    def deep_link(self) -> str | None:
        """YouTube URL seeked to the moment this passage is spoken.

        This is what turns a citation from a claim into something the reader can
        check in one click.
        """
        if not self.episode.youtube_url:
            return None
        if self.start_seconds is None:
            return self.episode.youtube_url
        separator = "&" if "?" in self.episode.youtube_url else "?"
        return f"{self.episode.youtube_url}{separator}t={self.start_seconds}s"


class Citation(BaseModel):
    """A source attached to an answer.

    Only ever constructed from a chunk that retrieval actually returned — see
    ``retrieval/citations.py``. The model is never permitted to invent one.
    """

    marker: str = Field(description="In-text marker, e.g. 'S1'.")
    chunk_id: int
    episode_slug: str
    guest: str | None = None
    title: str
    youtube_url: str | None = None
    deep_link: str | None = None
    start_seconds: int | None = None
    speaker: str | None = None
    quote: str = Field(default="", description="Short excerpt shown in the UI.")
    score: float = 0.0


# --- Conversation ------------------------------------------------------------


class Message(BaseModel):
    id: UUID
    session_id: UUID
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


class SessionSummary(BaseModel):
    """Row in the conversation sidebar."""

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    last_message_preview: str | None = None


class SessionDetail(BaseModel):
    id: UUID
    title: str
    user_id: str
    user_metadata: dict[str, Any] = Field(default_factory=dict)
    model_provider: str | None = None
    model_name: str | None = None
    created_at: datetime
    updated_at: datetime
    messages: list[Message] = Field(default_factory=list)


class Artifact(BaseModel):
    id: UUID
    session_id: UUID
    message_id: UUID | None = None
    kind: ArtifactKind
    title: str
    content: str
    version: int
    safety_report: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
