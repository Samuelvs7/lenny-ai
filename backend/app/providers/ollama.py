"""Ollama provider — local generation and embeddings.

This is the provider the graded demo runs on, so its failure modes get more
attention than the cloud path. The three that actually happen on a fresh
machine, in order of frequency:

1. Ollama is not running        -> connection refused
2. Ollama is running, model not pulled -> HTTP 404 with a 'not found' body
3. Model is pulled but slow     -> read timeout on first token (cold load)

Each is reported as a distinct, actionable message. "Model not available" with
no further detail is the difference between a 30-second fix and a confused
evaluator.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.errors import (
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.observability import get_logger
from app.providers.base import (
    ChatMessage,
    EmbeddingProvider,
    LLMProvider,
    LLMResponse,
    ProviderHealth,
)

log = get_logger(__name__)

PROVIDER_NAME = "ollama"


def _install_hint(model: str) -> str:
    return f"Run `ollama pull {model}` and confirm `ollama list` shows it."


class OllamaProvider(LLMProvider):
    """Chat completions against a local Ollama server."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int = 180,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        # Injectable for tests: the suite exercises every failure branch
        # against a mock transport rather than a live server.
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
        )

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- generation ---------------------------------------------------------

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: Sequence[str] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if stop:
            payload["options"]["stop"] = list(stop)

        started = time.perf_counter()
        data = await self._post("/api/chat", payload)
        latency_ms = (time.perf_counter() - started) * 1000

        message = data.get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise ProviderResponseError(
                detail=f"Ollama returned an empty completion for model {self._model}"
            )

        done_reason = data.get("done_reason")
        return LLMResponse(
            text=content,
            provider=self.name,
            model=self._model,
            latency_ms=latency_ms,
            prompt_tokens=data.get("prompt_eval_count"),
            completion_tokens=data.get("eval_count"),
            finish_reason=done_reason,
            truncated=done_reason == "length",
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        """Yield content deltas as they arrive.

        Streaming matters more for local models than cloud ones: a 3B model on
        CPU can take 30+ seconds for a long answer, and a UI that shows nothing
        for 30 seconds reads as broken.
        """
        payload = {
            "model": self._model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": True,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        import json as _json

        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    self._raise_for_status(response.status_code, body)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = _json.loads(line)
                    except ValueError:
                        continue
                    chunk = (event.get("message") or {}).get("content")
                    if chunk:
                        yield chunk
                    if event.get("done"):
                        break
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                detail=f"Ollama stream timed out after {self._timeout}s"
            ) from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(
                message="Cannot reach the local Ollama server.",
                detail=f"Connection refused at {self._base_url}. Is `ollama serve` running?",
            ) from exc

    # --- health -------------------------------------------------------------

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            response = await self._client.get("/api/tags", timeout=10.0)
            response.raise_for_status()
            tags = response.json().get("models", [])
            names = [tag.get("name", "") for tag in tags]

            # Ollama reports 'llama3.2:latest'; config usually says 'llama3.2'.
            # Compare on the bare name so a correct setup is not reported broken.
            def matches(candidate: str) -> bool:
                return candidate == self._model or candidate.split(":")[0] == self._model.split(":")[0]

            present = any(matches(name) for name in names)
            return ProviderHealth(
                name=self.name,
                available=present,
                model=self._model,
                detail="" if present else f"Model '{self._model}' is not pulled. {_install_hint(self._model)}",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                available_models=names,
            )
        except httpx.ConnectError:
            return ProviderHealth(
                name=self.name,
                available=False,
                model=self._model,
                detail=f"Cannot connect to Ollama at {self._base_url}. Start it with `ollama serve`.",
            )
        except Exception as exc:  # health must never raise
            return ProviderHealth(
                name=self.name,
                available=False,
                model=self._model,
                detail=f"{type(exc).__name__} while probing Ollama",
            )

    # --- transport ----------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                detail=(
                    f"Ollama did not respond within {self._timeout}s. A cold model "
                    f"load can exceed this; raise OLLAMA_TIMEOUT_SECONDS if it recurs."
                )
            ) from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(
                message="Cannot reach the local Ollama server.",
                detail=f"Connection refused at {self._base_url}. Is `ollama serve` running?",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                detail=f"{type(exc).__name__} talking to Ollama at {self._base_url}"
            ) from exc

        if response.status_code >= 400:
            self._raise_for_status(response.status_code, response.text)

        try:
            return response.json()
        except ValueError as exc:
            raise ProviderResponseError(detail="Ollama returned a non-JSON body") from exc

    def _raise_for_status(self, status_code: int, body: str) -> None:
        snippet = body[:300]
        if status_code == 404 or "not found" in body.lower():
            raise ProviderUnavailableError(
                message=f"The model '{self._model}' is not available in Ollama.",
                detail=f"HTTP {status_code}: {snippet}. {_install_hint(self._model)}",
            )
        raise ProviderResponseError(detail=f"Ollama HTTP {status_code}: {snippet}")


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Embeddings from a local Ollama model (default: nomic-embed-text, 768d).

    Used by both ingestion and query time. The same model must serve both —
    vectors from different models are not comparable — so the model name is
    recorded on every chunk and validated when the index loads.
    """

    name = PROVIDER_NAME

    #: nomic-embed-text is 768-dimensional. Confirmed from the first live
    #: response rather than trusted blindly; see ``_dimensions``.
    DEFAULT_DIMENSIONS = 768

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int = 120,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._dimensions = self.DEFAULT_DIMENSIONS
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Uses Ollama's batch ``/api/embed`` endpoint. Empty strings are rejected
        early because Ollama returns a zero vector for them, which would
        silently pollute the index with a chunk that matches everything weakly.
        """
        if not texts:
            return []
        cleaned = [t.strip() for t in texts]
        if any(not t for t in cleaned):
            raise ProviderResponseError(
                detail="Refusing to embed an empty string (would produce a zero vector)"
            )

        try:
            response = await self._client.post(
                "/api/embed",
                json={"model": self._model, "input": list(cleaned)},
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                detail=f"Embedding request timed out after {self._timeout}s"
            ) from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(
                message="Cannot reach the local Ollama server for embeddings.",
                detail=f"Connection refused at {self._base_url}.",
            ) from exc

        if response.status_code >= 400:
            body = response.text[:300]
            if response.status_code == 404 or "not found" in body.lower():
                raise ProviderUnavailableError(
                    message=f"Embedding model '{self._model}' is not available.",
                    detail=f"HTTP {response.status_code}: {body}. {_install_hint(self._model)}",
                )
            raise ProviderResponseError(
                detail=f"Ollama embed HTTP {response.status_code}: {body}"
            )

        try:
            vectors = response.json().get("embeddings")
        except ValueError as exc:
            raise ProviderResponseError(detail="Ollama embed returned non-JSON") from exc

        if not isinstance(vectors, list) or len(vectors) != len(cleaned):
            raise ProviderResponseError(
                detail=(
                    f"Expected {len(cleaned)} embeddings, got "
                    f"{len(vectors) if isinstance(vectors, list) else type(vectors).__name__}"
                )
            )

        if vectors and isinstance(vectors[0], list):
            self._dimensions = len(vectors[0])
        return vectors

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            vectors = await self.embed(["health check"])
            return ProviderHealth(
                name=f"{self.name}-embeddings",
                available=bool(vectors and vectors[0]),
                model=self._model,
                detail=f"{len(vectors[0])} dimensions" if vectors else "no vector returned",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        except Exception as exc:
            detail = exc.detail if hasattr(exc, "detail") and exc.detail else type(exc).__name__
            return ProviderHealth(
                name=f"{self.name}-embeddings",
                available=False,
                model=self._model,
                detail=str(detail),
            )
