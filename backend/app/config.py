"""Application configuration.

All runtime configuration is read from the environment exactly once, validated
here, and injected everywhere else. Nothing in the codebase reads ``os.environ``
directly — that keeps the set of knobs discoverable and makes the "switch the
model without touching application code" requirement a property of the design
rather than a promise.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderName(str, Enum):
    """Model providers the application knows how to construct."""

    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    NONE = "none"


class AgentRunner(str, Enum):
    """Agent execution backends.

    ``NATIVE`` is provider-agnostic and drives the mandatory local-model demo.
    ``CLAUDE_AGENT_SDK`` runs the same skills through Anthropic's official SDK.
    """

    NATIVE = "native"
    CLAUDE_AGENT_SDK = "claude_agent_sdk"


class LogFormat(str, Enum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """Validated application settings. See ``.env.example`` for documentation."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database -----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://lenny:lenny@localhost:5432/lenny",
        description="Async SQLAlchemy/asyncpg connection URL.",
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_pool_max_overflow: int = Field(default=5, ge=0, le=50)
    db_connect_timeout_seconds: int = Field(default=10, ge=1, le=120)

    # --- Model providers ----------------------------------------------------
    model_provider: ProviderName = ProviderName.OLLAMA
    model_fallback_provider: ProviderName = ProviderName.NONE

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_timeout_seconds: int = Field(default=600, ge=5, le=1800)

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    anthropic_timeout_seconds: int = Field(default=120, ge=5, le=900)

    agent_runner: AgentRunner = AgentRunner.NATIVE

    # --- Retrieval ----------------------------------------------------------
    retrieval_candidates: int = Field(default=40, ge=5, le=500)
    retrieval_top_k: int = Field(default=5, ge=1, le=50)
    retrieval_min_similarity: float = Field(default=0.55, ge=0.0, le=1.0)

    # --- Ingestion ----------------------------------------------------------
    ingest_repo_url: str = "https://github.com/ChatPRD/lennys-podcast-transcripts"
    ingest_repo_ref: str = "main"
    ingest_max_episodes: int = Field(default=30, ge=0)
    ingest_chunk_words: int = Field(default=320, ge=50, le=2000)
    ingest_chunk_overlap_words: int = Field(default=60, ge=0, le=500)
    ingest_strip_sponsors: bool = True

    # --- Server / observability --------------------------------------------
    app_env: str = "development"
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: str = "http://localhost:5173,http://localhost:4173"

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}")
        return level

    @field_validator("ingest_chunk_overlap_words")
    @classmethod
    def _overlap_must_be_smaller_than_chunk(cls, value: int, info) -> int:
        chunk = info.data.get("ingest_chunk_words")
        if chunk is not None and value >= chunk:
            raise ValueError(
                "INGEST_CHUNK_OVERLAP_WORDS must be smaller than INGEST_CHUNK_WORDS, "
                f"got overlap={value} chunk={chunk}"
            )
        return value

    # --- Derived helpers ----------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_anthropic_configured(self) -> bool:
        """True when a cloud call could plausibly succeed.

        Checked before constructing the provider so a missing key produces a
        clear startup warning instead of an opaque 401 mid-conversation.
        """
        return bool(self.anthropic_api_key.strip())

    def describe_safe(self) -> dict[str, object]:
        """Config snapshot safe to log or expose.

        Secrets are reduced to a boolean. Nothing here should ever contain a
        key, password, or connection string with credentials in it.
        """
        return {
            "app_env": self.app_env,
            "model_provider": self.model_provider.value,
            "model_fallback_provider": self.model_fallback_provider.value,
            "agent_runner": self.agent_runner.value,
            "ollama_base_url": self.ollama_base_url,
            "ollama_model": self.ollama_model,
            "ollama_embed_model": self.ollama_embed_model,
            "anthropic_model": self.anthropic_model,
            "anthropic_api_key_configured": self.is_anthropic_configured,
            "database_host": _safe_db_host(self.database_url),
            "retrieval_top_k": self.retrieval_top_k,
            "retrieval_min_similarity": self.retrieval_min_similarity,
        }


def _safe_db_host(database_url: str) -> str:
    """Extract host:port/name from a DSN, dropping any embedded credentials."""
    try:
        after_scheme = database_url.split("://", 1)[1]
        location = after_scheme.rsplit("@", 1)[-1]  # strip user:password@
        return location
    except (IndexError, AttributeError):
        return "unparsed"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that validation runs once and every component observes identical
    configuration. Tests clear the cache via ``get_settings.cache_clear()``.
    """
    return Settings()
