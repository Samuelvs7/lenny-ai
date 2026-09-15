"""Anthropic provider — the cloud generation path.

Uses the official ``anthropic`` SDK rather than raw HTTP, so retries, timeouts
and typed exceptions come from the vendor rather than being reimplemented here.

Two model-behaviour details that are easy to get wrong and are handled
explicitly below:

* **Sampling parameters are rejected.** Current models (Opus 5 and the 4.6+
  family) return HTTP 400 if ``temperature`` / ``top_p`` / ``top_k`` are sent.
  Our interface accepts a ``temperature`` because Ollama uses it; this provider
  deliberately drops it. Determinism is instead controlled through prompting
  and through the validators that check each skill's output.
* **Responses contain more than text.** ``response.content`` is a list of typed
  blocks (thinking, text, tool_use). Thinking is on by default on Opus 5, so
  naively reading ``content[0].text`` can return an empty string. We filter to
  text blocks.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from app.errors import (
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.observability import get_logger
from app.providers.base import (
    ChatMessage,
    LLMProvider,
    LLMResponse,
    ProviderHealth,
    Role,
)

log = get_logger(__name__)

PROVIDER_NAME = "anthropic"


class AnthropicProvider(LLMProvider):
    """Chat completions via the Anthropic Messages API."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int = 120,
        client: Any | None = None,
    ) -> None:
        if not api_key and client is None:
            # Fail at construction, not at first use. Registry checks this so a
            # missing key surfaces as a clear startup warning and a disabled
            # provider in the UI, never as a 401 halfway through a demo.
            raise ProviderUnavailableError(
                message="The Anthropic provider is not configured.",
                detail="ANTHROPIC_API_KEY is empty. Set it in .env, or run with MODEL_PROVIDER=ollama.",
            )

        self._model = model
        self._timeout = timeout_seconds

        if client is not None:
            # Injected by tests to exercise every branch without a network call.
            self._client = client
        else:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise ProviderUnavailableError(
                    message="The Anthropic SDK is not installed.",
                    detail="Install it with `pip install anthropic`.",
                ) from exc
            self._client = AsyncAnthropic(api_key=api_key, timeout=float(timeout_seconds))

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()

    # --- generation ---------------------------------------------------------

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,  # noqa: ARG002 - see module docstring
        stop: Sequence[str] | None = None,
    ) -> LLMResponse:
        system_prompt, turns = _split_system(messages)

        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": turns,
        }
        if system_prompt:
            request["system"] = system_prompt
        if stop:
            request["stop_sequences"] = list(stop)

        started = time.perf_counter()
        try:
            response = await self._client.messages.create(**request)
        except Exception as exc:
            raise _translate_error(exc, model=self._model) from exc
        latency_ms = (time.perf_counter() - started) * 1000

        # Safety classifiers can decline a request with HTTP 200. Check before
        # reading content, which is empty in that case.
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderResponseError(
                message="The cloud model declined to answer this request.",
                detail=f"stop_reason=refusal category={category}",
            )

        text = _extract_text(response)
        if not text:
            raise ProviderResponseError(
                detail=f"No text content in response (stop_reason={stop_reason})"
            )

        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=getattr(response, "model", self._model),
            latency_ms=latency_ms,
            prompt_tokens=getattr(usage, "input_tokens", None) if usage else None,
            completion_tokens=getattr(usage, "output_tokens", None) if usage else None,
            finish_reason=stop_reason,
            truncated=stop_reason == "max_tokens",
        )

    # --- health -------------------------------------------------------------

    async def health(self) -> ProviderHealth:
        """Probe with a minimal billed request.

        A one-token completion is the cheapest call that proves the key, the
        model name and network egress all work. Listing models would not catch
        an invalid key scope.
        """
        started = time.perf_counter()
        try:
            await self._client.messages.create(
                model=self._model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return ProviderHealth(
                name=self.name,
                available=True,
                model=self._model,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        except Exception as exc:
            translated = _translate_error(exc, model=self._model)
            return ProviderHealth(
                name=self.name,
                available=False,
                model=self._model,
                detail=translated.detail or translated.message,
            )


# --- helpers -----------------------------------------------------------------


def _split_system(messages: Sequence[ChatMessage]) -> tuple[str, list[dict[str, str]]]:
    """Separate system content from the conversation.

    The Messages API takes the system prompt as a top-level parameter, not as a
    message. Multiple system messages are concatenated so callers can compose a
    prompt from several fragments without knowing this detail.
    """
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for message in messages:
        if message.role is Role.SYSTEM:
            system_parts.append(message.content)
        else:
            turns.append({"role": message.role.value, "content": message.content})
    return "\n\n".join(system_parts), turns


def _extract_text(response: Any) -> str:
    """Concatenate text blocks, ignoring thinking and tool-use blocks."""
    blocks = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in blocks:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


def _translate_error(exc: Exception, *, model: str):
    """Map SDK exceptions onto our provider error types.

    Ordered most-specific first. Collapsing these into one broad handler would
    lose the retryable/non-retryable distinction the UI depends on.
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover - dependency guard
        return ProviderUnavailableError(detail=f"{type(exc).__name__}: {exc}")

    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeoutError(detail=f"Anthropic request timed out: {exc}")
    if isinstance(exc, anthropic.AuthenticationError):
        return ProviderUnavailableError(
            message="The Anthropic API key was rejected.",
            detail="401 authentication_error — check ANTHROPIC_API_KEY.",
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return ProviderUnavailableError(
            message="This Anthropic key is not permitted to use that model.",
            detail=f"403 permission_error for model {model}.",
        )
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderUnavailableError(
            message=f"The model '{model}' is not available to this account.",
            detail="404 not_found_error — check ANTHROPIC_MODEL.",
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderUnavailableError(
            message="The cloud model is rate limited right now.",
            detail="429 rate_limit_error — retry shortly or switch to MODEL_PROVIDER=ollama.",
        )
    if isinstance(exc, anthropic.BadRequestError):
        # Non-retryable: usually a malformed request on our side.
        return ProviderResponseError(
            message="The request to the cloud model was rejected.",
            detail=f"400 invalid_request_error: {exc}",
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailableError(
            message="Could not reach the Anthropic API.",
            detail=f"Network error: {type(exc).__name__}",
        )
    if isinstance(exc, anthropic.APIStatusError):
        return ProviderResponseError(
            detail=f"Anthropic HTTP {getattr(exc, 'status_code', '?')}: {exc}"
        )
    return ProviderResponseError(detail=f"{type(exc).__name__}: {exc}")
