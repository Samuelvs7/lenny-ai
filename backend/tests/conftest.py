"""Shared test fixtures.

The suite is split into two tiers:

* **Pure tests** — parsing, sponsor detection, chunking, routing, citation
  validation, artifact sanitisation, provider error mapping. No database, no
  model, no network. These are the bulk of the suite and run in about a second.
* **Integration tests** — API contracts and persistence against a real
  PostgreSQL. They are *skipped with a clear reason* when ``TEST_DATABASE_URL``
  is unset, so ``pytest`` still passes on a machine without a database rather
  than erroring in a way that looks like broken code.

Model providers are always faked. Asserting on a local model's prose would be
testing llama3.2, not this application; what we test is that the pipeline
around it behaves — routing, grounding gates, citation validation, persistence,
and failure handling.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import pytest

from app.config import Settings, get_settings
from app.providers.base import (
    ChatMessage,
    EmbeddingProvider,
    LLMProvider,
    LLMResponse,
    ProviderHealth,
)

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason=(
        "Set TEST_DATABASE_URL to run database-backed tests, e.g. "
        "postgresql+asyncpg://lenny:lenny@localhost:5432/lenny_test"
    ),
)


class FakeLLM(LLMProvider):
    """Scripted model. Returns queued replies and records what it was asked."""

    name = "fake"

    def __init__(self, replies: Sequence[str] | None = None, *, model: str = "fake-1") -> None:
        self._replies = list(replies or ["a grounded answer [S1]"])
        self._model = model
        self.calls: list[list[ChatMessage]] = []
        self.available = True

    @property
    def model(self) -> str:
        return self._model

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: Sequence[str] | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        text = self._replies.pop(0) if self._replies else "fallback reply"
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self._model,
            latency_ms=1.0,
            prompt_tokens=10,
            completion_tokens=5,
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, available=self.available, model=self._model)


class FailingLLM(LLMProvider):
    """Model that always raises a given error — for failure-path tests."""

    name = "failing"

    def __init__(self, error: Exception) -> None:
        self._error = error

    @property
    def model(self) -> str:
        return "failing-1"

    async def generate(self, messages, **kwargs) -> LLMResponse:  # noqa: ANN001
        raise self._error

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name=self.name, available=False, model="failing-1", detail="always fails"
        )


class FakeEmbedder(EmbeddingProvider):
    """Deterministic embeddings.

    Vectors are derived from token hashes, so semantically identical text gets
    identical vectors and different text gets different ones — enough to
    exercise index and fusion logic without running a model.
    """

    name = "fake-embed"

    def __init__(self, dimensions: int = 8) -> None:
        self._dimensions = dimensions

    @property
    def model(self) -> str:
        return "fake-embed-1"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self._dimensions
            for token in text.lower().split():
                vector[hash(token) % self._dimensions] += 1.0
            vectors.append(vector)
        return vectors

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, available=True, model=self.model)


@pytest.fixture
def settings() -> Settings:
    """Settings isolated from the developer's own .env."""
    get_settings.cache_clear()
    return Settings(
        database_url=TEST_DATABASE_URL or "postgresql+asyncpg://x:x@localhost:5432/x",
        model_provider="ollama",
        retrieval_top_k=5,
        retrieval_min_similarity=0.55,
        log_format="console",
        log_level="WARNING",
        app_env="test",
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()
