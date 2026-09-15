"""Structured error taxonomy.

Every failure the client can observe is one of these types. Each carries:

* ``code``      — a stable machine-readable string the frontend switches on.
* ``message``   — plain language, shown to the user. Must never contain a
                  secret, a connection string, or a raw stack trace.
* ``detail``    — developer-facing context, surfaced in logs always and in the
                  response body only outside production.
* ``retryable`` — whether retrying the same request could plausibly succeed.
                  Drives whether the UI offers a "Try again" button.

The split between ``message`` and ``detail`` is deliberate: the brief asks for
errors that are understandable to users *and* useful to developers, and those
are usually not the same sentence.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for all expected, handled failures."""

    code: str = "internal_error"
    http_status: int = 500
    retryable: bool = False
    #: Shown to the user when no more specific message is supplied.
    default_message: str = "Something went wrong on our side."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.detail = detail
        self.context = context or {}
        super().__init__(self.message)

    def to_payload(self, *, request_id: str, include_detail: bool) -> dict[str, Any]:
        """Render the JSON body returned to the client."""
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "request_id": request_id,
        }
        if include_detail and self.detail:
            error["detail"] = self.detail
        return {"error": error}


# --- Client-side problems ----------------------------------------------------


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404
    default_message = "That resource does not exist."


class InvalidRequestError(AppError):
    code = "invalid_request"
    http_status = 400
    default_message = "The request was not valid."


# --- Infrastructure ----------------------------------------------------------


class DatabaseUnavailableError(AppError):
    """Postgres is unreachable, refusing connections, or timed out."""

    code = "database_unavailable"
    http_status = 503
    retryable = True
    default_message = (
        "The conversation store is unreachable, so this chat cannot be saved right now. "
        "Please try again in a moment."
    )


# --- Model providers ---------------------------------------------------------


class ProviderError(AppError):
    """Base for anything that goes wrong talking to a language model."""

    code = "provider_error"
    http_status = 502
    retryable = True
    default_message = "The language model could not complete this request."


class ProviderUnavailableError(ProviderError):
    """The provider cannot be reached or is not configured.

    Covers: Ollama not running, model not pulled, missing API key.
    """

    code = "provider_unavailable"
    http_status = 503
    retryable = True
    default_message = "The selected model is not available right now."


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    http_status = 504
    retryable = True
    default_message = (
        "The model took too long to respond. Local models can be slow on CPU — "
        "try a shorter question, or switch provider."
    )


class ProviderResponseError(ProviderError):
    """The provider replied, but not with anything usable.

    Small local models genuinely do emit truncated JSON and empty completions,
    so this is an expected branch rather than an exceptional one.
    """

    code = "provider_bad_response"
    http_status = 502
    retryable = True
    default_message = "The model returned a response we could not read. Please try again."


# --- Retrieval & grounding ---------------------------------------------------


class RetrievalUnavailableError(AppError):
    """The knowledge base is empty or the index failed to load."""

    code = "retrieval_unavailable"
    http_status = 503
    retryable = False
    default_message = (
        "The transcript knowledge base has not been loaded yet. "
        "Run the ingestion step before asking questions."
    )


class InsufficientGroundingError(AppError):
    """Retrieval succeeded but nothing relevant enough came back.

    Not a bug — this is the assistant correctly declining to answer from
    material it does not have. Surfaced as a normal assistant turn, not an
    error toast, but modelled as a typed condition so the behaviour is
    testable and observable.
    """

    code = "insufficient_grounding"
    http_status = 200
    retryable = False
    default_message = (
        "I could not find anything in the Lenny's Podcast transcripts that supports "
        "an answer to that."
    )


# --- Skills & artifacts ------------------------------------------------------


class SkillExecutionError(AppError):
    code = "skill_failed"
    http_status = 500
    retryable = True
    default_message = "The assistant could not complete that task."


class ArtifactGenerationError(AppError):
    code = "artifact_generation_failed"
    http_status = 500
    retryable = True
    default_message = "The artifact could not be generated. Please try again."


class UnsafeArtifactError(AppError):
    """Generated artifact content was rejected by the safety layer.

    Deliberately a distinct code so it is visible in logs and in the UI: a
    blocked artifact is a security event worth seeing, not a generic failure.
    """

    code = "artifact_unsafe"
    http_status = 422
    retryable = True
    default_message = "The generated artifact was blocked because it contained unsafe content."
