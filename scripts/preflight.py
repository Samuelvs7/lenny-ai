#!/usr/bin/env python3
"""Preflight check - run this before anything else.

Verifies that this machine can actually run The Lenny Growth Assistant, and for
anything missing prints the exact command to fix it.

    python scripts/preflight.py

Exits 0 if everything required is present, 1 otherwise. Optional items are
reported but never fail the check - the app is designed to run fully local
without a cloud key.

Deliberately dependency-free: it uses only the standard library, so it works
before `pip install` and tells you *that* you need to run it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

OK, WARN, FAIL = "  OK  ", " WARN ", " FAIL "
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    print(f"[{status}] {name}" + (f"\n         {detail}" if detail else ""))


def read_env() -> dict[str, str]:
    """Parse .env without requiring python-dotenv."""
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def check_python() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        record(OK, f"Python {major}.{minor}")
    else:
        record(FAIL, f"Python {major}.{minor}", "Python 3.11+ required.")


def check_node() -> None:
    node = shutil.which("node")
    if not node:
        record(WARN, "Node.js not found", "Needed only to run the frontend. Install Node 20+.")
        return
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        major = int(version.lstrip("v").split(".")[0])
        if major >= 20:
            record(OK, f"Node.js {version}")
        else:
            record(WARN, f"Node.js {version}", "Node 20+ recommended for the frontend build.")
    except Exception as exc:
        record(WARN, "Node.js version unreadable", str(exc))


def check_env_file() -> dict[str, str]:
    if (ROOT / ".env").exists():
        record(OK, ".env present")
    else:
        record(
            FAIL,
            ".env missing",
            "Run:  cp .env.example .env    (defaults work for a local setup)",
        )
    return read_env()


def check_no_committed_secrets() -> None:
    """A committed .env is the failure that matters most. Check it explicitly."""
    try:
        tracked = subprocess.run(
            ["git", "ls-files", ".env"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if tracked:
            record(FAIL, ".env is tracked by git", "Run:  git rm --cached .env")
        else:
            record(OK, ".env is not tracked by git")
    except Exception:
        record(WARN, "git not available", "Could not verify .env is untracked.")


def check_postgres(env: dict[str, str]) -> None:
    url = env.get("DATABASE_URL") or os.getenv("DATABASE_URL", "")
    if not url:
        record(FAIL, "DATABASE_URL not set", "Add it to .env.")
        return

    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    host, port = parsed.hostname or "localhost", parsed.port or 5432

    # A TCP probe, not a driver connect: this script must work before
    # `pip install`, and "is something listening" is the question that
    # distinguishes "Postgres is down" from "credentials are wrong".
    try:
        with socket.create_connection((host, port), timeout=5):
            record(OK, f"PostgreSQL reachable at {host}:{port}")
    except OSError:
        record(
            FAIL,
            f"PostgreSQL not reachable at {host}:{port}",
            "Start it:  docker compose up -d postgres\n"
            "         or start your local PostgreSQL service.",
        )


def check_ollama(env: dict[str, str]) -> None:
    base = (env.get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
    chat = env.get("OLLAMA_MODEL", "llama3.2")
    embed = env.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        record(
            FAIL,
            f"Ollama not reachable at {base}",
            "Start it:  ollama serve      (or: docker compose up -d ollama)",
        )
        return

    record(OK, f"Ollama reachable at {base}")

    installed = [m.get("name", "") for m in payload.get("models", [])]
    for label, wanted in (("chat", chat), ("embedding", embed)):
        # Ollama reports 'llama3.2:latest'; config usually says 'llama3.2'.
        present = any(
            name == wanted or name.split(":")[0] == wanted.split(":")[0]
            for name in installed
        )
        if present:
            record(OK, f"{label} model '{wanted}' pulled")
        else:
            record(
                FAIL,
                f"{label} model '{wanted}' not pulled",
                f"Run:  ollama pull {wanted}",
            )


def check_cloud_provider(env: dict[str, str]) -> None:
    key = env.get("ANTHROPIC_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
    if key.strip():
        record(OK, "ANTHROPIC_API_KEY set", "Cloud provider available (MODEL_PROVIDER=anthropic).")
    else:
        record(
            WARN,
            "ANTHROPIC_API_KEY not set",
            "Optional. The app runs fully local on Ollama without it.",
        )


def check_knowledge_base(env: dict[str, str]) -> None:
    """Ask the API, if it is running. Absence is informational, not a failure."""
    port = env.get("API_PORT", "8000")
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/api/knowledge-base", timeout=5) as r:
            stats = json.loads(r.read().decode("utf-8"))
    except Exception:
        record(
            WARN,
            "API not running",
            "Start it, then re-run this check to see knowledge-base status.",
        )
        return

    episodes, chunks = stats.get("episodes", 0), stats.get("chunks", 0)
    if chunks == 0:
        record(
            FAIL,
            "Knowledge base is empty",
            "Run:  python -m app.ingestion --episodes 8\n"
            "      (or: docker compose run --rm ingest --episodes 8)",
        )
    else:
        embedded = stats.get("chunks_with_embeddings", 0)
        note = "" if embedded else "  No embeddings - retrieval will be lexical-only."
        record(OK, f"Knowledge base: {episodes} episodes, {chunks} passages", note.strip())


def main() -> int:
    print("\nThe Lenny Growth Assistant - preflight\n" + "=" * 46)
    check_python()
    check_node()
    env = check_env_file()
    check_no_committed_secrets()
    check_postgres(env)
    check_ollama(env)
    check_cloud_provider(env)
    check_knowledge_base(env)

    failures = [name for status, name, _ in _results if status == FAIL]
    warnings = [name for status, name, _ in _results if status == WARN]

    print("\n" + "=" * 46)
    if failures:
        print(f"{len(failures)} blocking issue(s):")
        for name in failures:
            print(f"  - {name}")
        print("\nFix the above, then re-run:  python scripts/preflight.py")
        return 1

    if warnings:
        print(f"Ready to run. {len(warnings)} optional item(s) not configured:")
        for name in warnings:
            print(f"  - {name}")
    else:
        print("Everything checks out.")

    print("\nNext:  uvicorn app.main:app --port 8000   (from backend/)")
    print("       npm run dev                        (from frontend/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
