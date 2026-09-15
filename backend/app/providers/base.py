"""Model provider interface.

Everything above this module — router, skills, API — depends only on
:class:`LLMProvider`. Swapping Ollama for Anthropic is a configuration change
because no caller can observe which implementation it holds.

Two contracts are deliberately separate:

* :class:`LLMProvider`       — text generation.
* :class:`EmbeddingProvider` — vector embeddings.

They are split because the *right* pairing is usually mixed: the demo runs
generation on a cloud or local chat model while embeddings stay local and
free. Forcing one provider to do both would make that impossible.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(slots=True)
class LLMResponse:
    """A completed generation plus the metadata operations needs."""

    text: str
    provider: str
    model: str
    latency_ms: float
    #: Best-effort token accounting. Ollama reports real counts; cloud
    #: providers report usage. Never used for billing, only for observability.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    #: True when generation stopped because it hit the token ceiling. Callers
    #: that care about completeness (the essay skill) check this.
    truncated: bool = False


@dataclass(slots=True)
class ProviderHealth:
    """Result of probing a provider, for the deep health endpoint."""

    name: str
    available: bool
    model: str
    detail: str = ""
    latency_ms: float | None = None
    #: Models the backend reports it can serve. Lets the health endpoint say
    #: "Ollama is up but llama3.2 is not pulled", which is the single most
    #: common local-setup failure.
    available_models: list[str] = field(default_factory=list)


class LLMProvider(abc.ABC):
    """Text generation backend."""

    #: Stable identifier ('ollama', 'anthropic') stored on messages and shown
    #: in the UI so a transcript records which model produced which turn.
    name: str

    @property
    @abc.abstractmethod
    def model(self) -> str:
        """Model identifier currently configured for this provider."""

    @abc.abstractmethod
    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: Sequence[str] | None = None,
    ) -> LLMResponse:
        """Generate a completion.

        Implementations must raise only our provider error types
        (``ProviderUnavailableError``, ``ProviderTimeoutError``,
        ``ProviderResponseError``) so callers can handle failure uniformly
        regardless of which SDK or transport is underneath.
        """

    @abc.abstractmethod
    async def health(self) -> ProviderHealth:
        """Probe the backend. Must not raise; report failure in the result."""

    async def aclose(self) -> None:
        """Release transport resources. Default: nothing to do."""
        return None


@runtime_checkable
class StreamingLLMProvider(Protocol):
    """Optional capability: incremental token delivery.

    Kept optional rather than folded into :class:`LLMProvider` so a provider
    that cannot stream is simply not a ``StreamingLLMProvider`` — callers test
    with ``isinstance`` and fall back to a single ``generate`` call instead of
    implementations having to fake a stream.
    """

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        ...


class EmbeddingProvider(abc.ABC):
    """Vector embedding backend."""

    name: str

    @property
    @abc.abstractmethod
    def model(self) -> str: ...

    @property
    @abc.abstractmethod
    def dimensions(self) -> int:
        """Vector width. Used to validate the stored index still matches."""

    @abc.abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch. Order of results matches order of inputs."""

    @abc.abstractmethod
    async def health(self) -> ProviderHealth: ...

    async def aclose(self) -> None:
        return None
