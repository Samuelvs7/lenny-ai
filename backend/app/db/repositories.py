"""Persistence for conversations, messages, and artifacts.

Every public method translates driver exceptions into
:class:`DatabaseUnavailableError`. Callers above this layer never see
SQLAlchemy or asyncpg types, which keeps the "database unavailable" failure
path uniform and prevents connection strings leaking into error responses.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain import (
    Artifact,
    ArtifactKind,
    Citation,
    Message,
    MessageRole,
    SessionDetail,
    SessionSummary,
    SkillName,
)
from app.errors import DatabaseUnavailableError, NotFoundError
from app.observability import get_logger

log = get_logger(__name__)

#: Session titles are derived from the first user message; keep them short
#: enough to render in the sidebar without truncation mid-word.
_TITLE_MAX_CHARS = 60


def _wrap_db_errors(operation: str):
    """Decorator translating driver failures into our error type."""

    def decorator(func):
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except (SQLAlchemyError, DBAPIError, OSError) as exc:
                log.error(
                    "db.operation_failed",
                    operation=operation,
                    error_type=type(exc).__name__,
                )
                raise DatabaseUnavailableError(
                    detail=f"{operation} failed: {type(exc).__name__}"
                ) from exc

        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator


def _as_dict(value: Any) -> dict[str, Any]:
    """JSONB columns arrive as dict or str depending on driver settings."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def derive_title(first_user_message: str) -> str:
    """Turn the opening question into a sidebar label.

    Trimmed on a word boundary so the sidebar never shows a cut-off word.
    """
    cleaned = " ".join(first_user_message.split())
    if not cleaned:
        return "New chat"
    if len(cleaned) <= _TITLE_MAX_CHARS:
        return cleaned
    truncated = cleaned[:_TITLE_MAX_CHARS].rsplit(" ", 1)[0]
    return f"{truncated or cleaned[:_TITLE_MAX_CHARS]}…"


class SessionRepository:
    """Chat sessions and their messages."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    # --- sessions -----------------------------------------------------------

    @_wrap_db_errors("create_session")
    async def create(
        self,
        *,
        title: str = "New chat",
        user_id: str = "local-user",
        user_metadata: dict[str, Any] | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
    ) -> SessionDetail:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO sessions
                            (title, user_id, user_metadata, model_provider, model_name)
                        VALUES
                            (:title, :user_id, CAST(:user_metadata AS JSONB),
                             :model_provider, :model_name)
                        RETURNING id, title, user_id, user_metadata, model_provider,
                                  model_name, created_at, updated_at
                        """
                    ),
                    {
                        "title": title,
                        "user_id": user_id,
                        "user_metadata": json.dumps(user_metadata or {}),
                        "model_provider": model_provider,
                        "model_name": model_name,
                    },
                )
            ).mappings().one()

        return SessionDetail(
            id=row["id"],
            title=row["title"],
            user_id=row["user_id"],
            user_metadata=_as_dict(row["user_metadata"]),
            model_provider=row["model_provider"],
            model_name=row["model_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            messages=[],
        )

    @_wrap_db_errors("list_sessions")
    async def list_summaries(
        self, *, user_id: str = "local-user", limit: int = 50
    ) -> list[SessionSummary]:
        """Sidebar listing, newest activity first.

        The message count and preview are computed in one query via a lateral
        join rather than N+1 round-trips per session.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT s.id,
                               s.title,
                               s.created_at,
                               s.updated_at,
                               COALESCE(stats.message_count, 0) AS message_count,
                               stats.last_preview
                        FROM sessions s
                        LEFT JOIN LATERAL (
                            SELECT COUNT(*)::int AS message_count,
                                   (ARRAY_AGG(m.content ORDER BY m.created_at DESC))[1]
                                       AS last_preview
                            FROM messages m
                            WHERE m.session_id = s.id
                        ) stats ON TRUE
                        WHERE s.user_id = :user_id
                          AND s.archived_at IS NULL
                        ORDER BY s.updated_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"user_id": user_id, "limit": limit},
                )
            ).mappings().all()

        return [
            SessionSummary(
                id=row["id"],
                title=row["title"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                message_count=row["message_count"],
                last_message_preview=(
                    " ".join((row["last_preview"] or "").split())[:140] or None
                ),
            )
            for row in rows
        ]

    @_wrap_db_errors("get_session")
    async def get_detail(self, session_id: UUID) -> SessionDetail:
        """Load one session with its full message history, in order.

        Raises :class:`NotFoundError` for an unknown id — the API turns that
        into a 404 rather than an empty conversation, so a stale browser tab
        pointing at a deleted session says so.
        """
        async with self._engine.connect() as conn:
            session_row = (
                await conn.execute(
                    text(
                        """
                        SELECT id, title, user_id, user_metadata, model_provider,
                               model_name, created_at, updated_at
                        FROM sessions
                        WHERE id = :session_id
                        """
                    ),
                    {"session_id": session_id},
                )
            ).mappings().first()

            if session_row is None:
                raise NotFoundError(f"Session {session_id} was not found.")

            message_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT id, session_id, role, content, skill, router_reason,
                               model_provider, model_name, latency_ms, citations,
                               metadata, created_at
                        FROM messages
                        WHERE session_id = :session_id
                        ORDER BY created_at ASC
                        """
                    ),
                    {"session_id": session_id},
                )
            ).mappings().all()

        return SessionDetail(
            id=session_row["id"],
            title=session_row["title"],
            user_id=session_row["user_id"],
            user_metadata=_as_dict(session_row["user_metadata"]),
            model_provider=session_row["model_provider"],
            model_name=session_row["model_name"],
            created_at=session_row["created_at"],
            updated_at=session_row["updated_at"],
            messages=[_row_to_message(row) for row in message_rows],
        )

    @_wrap_db_errors("rename_session")
    async def rename(self, session_id: UUID, title: str) -> None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("UPDATE sessions SET title = :title WHERE id = :session_id"),
                {"title": title, "session_id": session_id},
            )
        if result.rowcount == 0:
            raise NotFoundError(f"Session {session_id} was not found.")

    @_wrap_db_errors("delete_session")
    async def delete(self, session_id: UUID) -> None:
        """Hard delete. Messages and artifacts cascade."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("DELETE FROM sessions WHERE id = :session_id"),
                {"session_id": session_id},
            )
        if result.rowcount == 0:
            raise NotFoundError(f"Session {session_id} was not found.")

    @_wrap_db_errors("exists_session")
    async def exists(self, session_id: UUID) -> bool:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT 1 FROM sessions WHERE id = :session_id"),
                    {"session_id": session_id},
                )
            ).first()
        return row is not None

    # --- messages -----------------------------------------------------------

    @_wrap_db_errors("add_message")
    async def add_message(
        self,
        *,
        session_id: UUID,
        role: MessageRole,
        content: str,
        skill: SkillName | None = None,
        router_reason: str | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        latency_ms: int | None = None,
        citations: Sequence[Citation] | None = None,
        metadata: dict[str, Any] | None = None,
        set_title_if_new: bool = False,
    ) -> Message:
        """Append a turn.

        Also bumps ``sessions.updated_at`` in the same transaction so the
        sidebar ordering can never disagree with the message history, and
        optionally sets the session title from the first user message.
        """
        citation_payload = [c.model_dump(mode="json") for c in (citations or [])]

        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO messages
                            (session_id, role, content, skill, router_reason,
                             model_provider, model_name, latency_ms,
                             citations, metadata)
                        VALUES
                            (:session_id, :role, :content, :skill, :router_reason,
                             :model_provider, :model_name, :latency_ms,
                             CAST(:citations AS JSONB), CAST(:metadata AS JSONB))
                        RETURNING id, session_id, role, content, skill, router_reason,
                                  model_provider, model_name, latency_ms, citations,
                                  metadata, created_at
                        """
                    ),
                    {
                        "session_id": session_id,
                        "role": role.value,
                        "content": content,
                        "skill": skill.value if skill else None,
                        "router_reason": router_reason,
                        "model_provider": model_provider,
                        "model_name": model_name,
                        "latency_ms": latency_ms,
                        "citations": json.dumps(citation_payload),
                        "metadata": json.dumps(metadata or {}),
                    },
                )
            ).mappings().one()

            # Touch the session so sidebar ordering stays correct. The trigger
            # only fires on UPDATE, so we issue one explicitly.
            if set_title_if_new:
                await conn.execute(
                    text(
                        """
                        UPDATE sessions
                        SET title = :title, updated_at = now()
                        WHERE id = :session_id AND title = 'New chat'
                        """
                    ),
                    {"title": derive_title(content), "session_id": session_id},
                )
            await conn.execute(
                text("UPDATE sessions SET updated_at = now() WHERE id = :session_id"),
                {"session_id": session_id},
            )

        return _row_to_message(row)

    @_wrap_db_errors("get_recent_messages")
    async def get_recent_messages(
        self, session_id: UUID, *, limit: int = 12
    ) -> list[Message]:
        """The last N turns, oldest-first, for conversational context.

        Bounded on purpose: a 3B local model has a small effective context, and
        replaying an entire long conversation crowds out the retrieved
        transcript passages that the answer actually needs to be grounded in.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT * FROM (
                            SELECT id, session_id, role, content, skill, router_reason,
                                   model_provider, model_name, latency_ms, citations,
                                   metadata, created_at
                            FROM messages
                            WHERE session_id = :session_id
                            ORDER BY created_at DESC
                            LIMIT :limit
                        ) recent
                        ORDER BY created_at ASC
                        """
                    ),
                    {"session_id": session_id, "limit": limit},
                )
            ).mappings().all()
        return [_row_to_message(row) for row in rows]


class ArtifactRepository:
    """Generated Markdown/HTML artifacts."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @_wrap_db_errors("create_artifact")
    async def create(
        self,
        *,
        session_id: UUID,
        message_id: UUID | None,
        kind: ArtifactKind,
        title: str,
        content: str,
        safety_report: dict[str, Any] | None = None,
    ) -> Artifact:
        """Insert a new artifact version.

        Version is computed per (session, title) so regenerating keeps history
        instead of overwriting what the user is currently reading.
        """
        async with self._engine.begin() as conn:
            next_version = (
                await conn.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(version), 0) + 1
                        FROM artifacts
                        WHERE session_id = :session_id AND title = :title
                        """
                    ),
                    {"session_id": session_id, "title": title},
                )
            ).scalar_one()

            row = (
                await conn.execute(
                    text(
                        """
                        INSERT INTO artifacts
                            (session_id, message_id, kind, title, content,
                             version, safety_report)
                        VALUES
                            (:session_id, :message_id, :kind, :title, :content,
                             :version, CAST(:safety_report AS JSONB))
                        RETURNING id, session_id, message_id, kind, title, content,
                                  version, safety_report, created_at
                        """
                    ),
                    {
                        "session_id": session_id,
                        "message_id": message_id,
                        "kind": kind.value,
                        "title": title,
                        "content": content,
                        "version": next_version,
                        "safety_report": json.dumps(safety_report or {}),
                    },
                )
            ).mappings().one()

        return _row_to_artifact(row)

    @_wrap_db_errors("get_artifact")
    async def get(self, artifact_id: UUID) -> Artifact:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT id, session_id, message_id, kind, title, content,
                               version, safety_report, created_at
                        FROM artifacts WHERE id = :artifact_id
                        """
                    ),
                    {"artifact_id": artifact_id},
                )
            ).mappings().first()
        if row is None:
            raise NotFoundError(f"Artifact {artifact_id} was not found.")
        return _row_to_artifact(row)

    @_wrap_db_errors("list_artifacts")
    async def list_for_session(self, session_id: UUID) -> list[Artifact]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT id, session_id, message_id, kind, title, content,
                               version, safety_report, created_at
                        FROM artifacts
                        WHERE session_id = :session_id
                        ORDER BY created_at DESC
                        """
                    ),
                    {"session_id": session_id},
                )
            ).mappings().all()
        return [_row_to_artifact(row) for row in rows]


# --- row mappers -------------------------------------------------------------


def _row_to_message(row: Any) -> Message:
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        role=MessageRole(row["role"]),
        content=row["content"],
        skill=SkillName(row["skill"]) if row["skill"] else None,
        router_reason=row["router_reason"],
        model_provider=row["model_provider"],
        model_name=row["model_name"],
        latency_ms=row["latency_ms"],
        citations=[Citation.model_validate(c) for c in _as_list(row["citations"])],
        metadata=_as_dict(row["metadata"]),
        created_at=row["created_at"],
    )


def _row_to_artifact(row: Any) -> Artifact:
    return Artifact(
        id=row["id"],
        session_id=row["session_id"],
        message_id=row["message_id"],
        kind=ArtifactKind(row["kind"]),
        title=row["title"],
        content=row["content"],
        version=row["version"],
        safety_report=_as_dict(row["safety_report"]),
        created_at=row["created_at"],
    )
