"""Provider construction, selection, and fallback.

This module is the only place that knows which concrete provider classes exist.
Everything else asks the registry for "the LLM" and gets back something
satisfying :class:`LLMProvider`.

Selection precedence, highest first:

1. A per-request override (the model picker in the UI).
2. ``MODEL_PROVIDER`` from the environment.

A provider that cannot be constructed — missing key, bad config — is recorded
as unavailable with the reason, rather than raising at import time. The app
must start even when the cloud provider is unconfigured, because running fully
local is the supported default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.config import ProviderName, Settings
from app.errors import ProviderError, ProviderUnavailableError
from app.observability import get_logger
from app.providers.anthropic import AnthropicProvider
from app.providers.base import (
    ChatMessage,
    EmbeddingProvider,
    LLMProvider,
    LLMResponse,
    ProviderHealth,
)
from app.providers.ollama import OllamaEmbeddingProvider, OllamaProvider

log = get_logger(__name__)


@dataclass(slots=True)
class ProviderSlot:
    """A provider that either was constructed, or explains why it was not."""

    name: str
    provider: LLMProvider | None
    unavailable_reason: str | None = None

    @property
    def is_available(self) -> bool:
        return self.provider is not None


class FallbackLLMProvider(LLMProvider):
    """Tries a primary provider, then a secondary on infrastructure failure.

    Only *infrastructure* failures fall through: unavailable, timeout, bad
    response. A refusal or a malformed request is not retried on the other
    provider, because the second attempt would fail identically and the user
    would wait twice as long to learn it.

    The response records the provider that actually served it, so the UI shows
    the truth rather than what was configured.
    """

    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self._primary = primary
        self._fallback = fallback
        self.name = primary.name

    @property
    def model(self) -> str:
        return self._primary.model

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: Sequence[str] | None = None,
    ) -> LLMResponse:
        try:
            return await self._primary.generate(
                messages, max_tokens=max_tokens, temperature=temperature, stop=stop
            )
        except ProviderError as exc:
            log.warning(
                "provider.fallback_engaged",
                primary=self._primary.name,
                fallback=self._fallback.name,
                reason=exc.code,
            )
            response = await self._fallback.generate(
                messages, max_tokens=max_tokens, temperature=temperature, stop=stop
            )
            return response

    async def health(self) -> ProviderHealth:
        primary = await self._primary.health()
        if primary.available:
            return primary
        fallback = await self._fallback.health()
        fallback.detail = (
            f"primary '{self._primary.name}' unavailable ({primary.detail}); "
            f"serving from fallback"
        )
        return fallback

    async def aclose(self) -> None:
        await self._primary.aclose()
        await self._fallback.aclose()


class ProviderRegistry:
    """Constructs and hands out model providers."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._slots: dict[str, ProviderSlot] = {}
        self._embedder: EmbeddingProvider | None = None
        self._build()

    # --- construction -------------------------------------------------------

    def _build(self) -> None:
        settings = self._settings

        # Ollama is always constructible: it needs no credentials, and whether
        # the server is actually running is a health question, not a
        # construction question.
        self._slots[ProviderName.OLLAMA.value] = ProviderSlot(
            name=ProviderName.OLLAMA.value,
            provider=OllamaProvider(
                base_url=settings.ollama_base_url,
                model=settings.ollama_model,
                timeout_seconds=settings.ollama_timeout_seconds,
            ),
        )

        if settings.is_anthropic_configured:
            try:
                self._slots[ProviderName.ANTHROPIC.value] = ProviderSlot(
                    name=ProviderName.ANTHROPIC.value,
                    provider=AnthropicProvider(
                        api_key=settings.anthropic_api_key,
                        model=settings.anthropic_model,
                        timeout_seconds=settings.anthropic_timeout_seconds,
                    ),
                )
            except ProviderUnavailableError as exc:
                self._slots[ProviderName.ANTHROPIC.value] = ProviderSlot(
                    name=ProviderName.ANTHROPIC.value,
                    provider=None,
                    unavailable_reason=exc.detail or exc.message,
                )
        else:
            self._slots[ProviderName.ANTHROPIC.value] = ProviderSlot(
                name=ProviderName.ANTHROPIC.value,
                provider=None,
                unavailable_reason=(
                    "ANTHROPIC_API_KEY is not set. Add it to .env to enable the cloud model."
                ),
            )

        self._embedder = OllamaEmbeddingProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_embed_model,
        )

        log.info(
            "providers.initialised",
            default=settings.model_provider.value,
            fallback=settings.model_fallback_provider.value,
            available=[name for name, slot in self._slots.items() if slot.is_available],
            unavailable=[name for name, slot in self._slots.items() if not slot.is_available],
        )

    # --- access -------------------------------------------------------------

    def get_llm(self, name: str | None = None) -> LLMProvider:
        """Return the requested provider, or the configured default.

        Wraps in :class:`FallbackLLMProvider` when a usable fallback is
        configured and differs from the primary.
        """
        requested = (name or self._settings.model_provider.value).lower()

        slot = self._slots.get(requested)
        if slot is None:
            raise ProviderUnavailableError(
                message=f"Unknown model provider '{requested}'.",
                detail=f"Known providers: {sorted(self._slots)}",
            )
        if slot.provider is None:
            raise ProviderUnavailableError(
                message=f"The '{requested}' model provider is not configured.",
                detail=slot.unavailable_reason,
            )

        primary = slot.provider
        fallback_name = self._settings.model_fallback_provider.value
        if fallback_name not in ("none", "", requested):
            fallback_slot = self._slots.get(fallback_name)
            if fallback_slot and fallback_slot.provider is not None:
                return FallbackLLMProvider(primary, fallback_slot.provider)
            log.warning(
                "provider.fallback_unavailable",
                configured=fallback_name,
                reason=fallback_slot.unavailable_reason if fallback_slot else "unknown provider",
            )
        return primary

    def get_embedder(self) -> EmbeddingProvider:
        if self._embedder is None:  # pragma: no cover - set in _build
            raise ProviderUnavailableError(detail="No embedding provider configured.")
        return self._embedder

    def describe(self) -> list[dict[str, object]]:
        """Provider inventory for the UI model picker and health endpoint."""
        default = self._settings.model_provider.value
        return [
            {
                "name": slot.name,
                "available": slot.is_available,
                "model": slot.provider.model if slot.provider else None,
                "is_default": slot.name == default,
                "reason": slot.unavailable_reason,
            }
            for slot in self._slots.values()
        ]

    async def health_all(self) -> list[ProviderHealth]:
        """Probe every constructible provider. Never raises."""
        results: list[ProviderHealth] = []
        for slot in self._slots.values():
            if slot.provider is None:
                results.append(
                    ProviderHealth(
                        name=slot.name,
                        available=False,
                        model="-",
                        detail=slot.unavailable_reason or "not configured",
                    )
                )
                continue
            results.append(await slot.provider.health())
        return results

    async def aclose(self) -> None:
        for slot in self._slots.values():
            if slot.provider is not None:
                await slot.provider.aclose()
        if self._embedder is not None:
            await self._embedder.aclose()
