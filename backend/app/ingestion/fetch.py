"""Fetching transcripts from the source repository.

Uses the GitHub REST API to list episodes and ``raw.githubusercontent.com`` to
download them. No git clone: the repository carries ~300 episodes and a shallow
HTTP fetch of just the files we want is faster, needs no git on the host, and
lets ingestion be resumed or scoped without re-downloading everything.

Responses are cached on disk so a re-run during development does not re-fetch
20 MB of text, and so ingestion can be re-run offline.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.observability import get_logger

log = get_logger(__name__)

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"


@dataclass(slots=True)
class EpisodeSource:
    """A transcript located in the source repository."""

    slug: str
    raw_url: str
    content: str

    @property
    def content_hash(self) -> str:
        """Stable digest used to skip unchanged episodes on re-ingestion."""
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _parse_repo(repo_url: str) -> tuple[str, str]:
    """``https://github.com/owner/name`` -> ``('owner', 'name')``."""
    cleaned = repo_url.rstrip("/").removesuffix(".git")
    parts = cleaned.split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse owner/repo from {repo_url!r}")
    return parts[-2], parts[-1]


class TranscriptFetcher:
    """Lists and downloads episode transcripts."""

    def __init__(
        self,
        *,
        repo_url: str,
        ref: str = "main",
        cache_dir: Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owner, self._repo = _parse_repo(repo_url)
        self._ref = ref
        self._cache_dir = cache_dir
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"User-Agent": "lenny-growth-assistant/1.0"},
            follow_redirects=True,
        )
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_episode_slugs(self) -> list[str]:
        """Directory names under ``episodes/``, alphabetically.

        Deterministic ordering matters: with ``INGEST_MAX_EPISODES`` set, the
        same subset is ingested every run, so retrieval results are
        reproducible between the evaluator's machine and ours.
        """
        url = f"{GITHUB_API}/repos/{self._owner}/{self._repo}/contents/episodes?ref={self._ref}"
        response = await self._client.get(url)
        if response.status_code == 403:
            raise RuntimeError(
                "GitHub API rate limit reached while listing episodes. "
                "Wait a few minutes, or set GITHUB_TOKEN in the environment."
            )
        response.raise_for_status()
        entries = response.json()
        return sorted(
            entry["name"] for entry in entries if entry.get("type") == "dir"
        )

    def _cache_path(self, slug: str) -> Path | None:
        return self._cache_dir / f"{slug}.md" if self._cache_dir else None

    async def fetch_episode(self, slug: str) -> EpisodeSource | None:
        """Download one transcript, using the on-disk cache when present."""
        raw_url = (
            f"{RAW_BASE}/{self._owner}/{self._repo}/{self._ref}/episodes/{slug}/transcript.md"
        )

        cache_path = self._cache_path(slug)
        if cache_path and cache_path.exists():
            content = cache_path.read_text(encoding="utf-8")
            if len(content) > 500:
                return EpisodeSource(slug=slug, raw_url=raw_url, content=content)

        try:
            response = await self._client.get(raw_url)
        except httpx.HTTPError as exc:
            log.warning("ingestion.fetch_failed", slug=slug, error=type(exc).__name__)
            return None

        if response.status_code == 404:
            log.warning("ingestion.transcript_missing", slug=slug)
            return None
        if response.status_code >= 400:
            log.warning("ingestion.fetch_http_error", slug=slug, status=response.status_code)
            return None

        content = response.text
        if len(content) < 500:
            # Guard against placeholder or truncated files becoming an
            # "episode" with no usable content.
            log.warning("ingestion.transcript_too_short", slug=slug, length=len(content))
            return None

        if cache_path:
            cache_path.write_text(content, encoding="utf-8")

        return EpisodeSource(slug=slug, raw_url=raw_url, content=content)
