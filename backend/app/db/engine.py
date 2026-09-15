"""Database engine, migrations, and connection health.

Schema lives in ``migrations/*.sql`` as plain SQL and is the single source of
truth. We deliberately do not mirror it in ORM model classes: two declarations
of the same table drift, and the SQL file is the artifact a client DBA will
actually read. Application code uses the async SQLAlchemy engine with explicit
statements, and repositories map rows to Pydantic DTOs.

Trade-off documented in architecture.md → "Why explicit SQL over an ORM".
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import Settings
from app.errors import DatabaseUnavailableError
from app.observability import get_logger, log_duration

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_engine: AsyncEngine | None = None


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine.

    ``pool_pre_ping`` matters here: a laptop demo gets suspended, Postgres drops
    idle connections, and without it the first request after a resume fails with
    a stale-connection error that looks like a bug in the app.
    """
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
        connect_args={"timeout": settings.db_connect_timeout_seconds},
    )


def get_engine(settings: Settings) -> AsyncEngine:
    """Process-wide engine singleton."""
    global _engine
    if _engine is None:
        _engine = create_engine(settings)
    return _engine


async def dispose_engine() -> None:
    """Close the pool on shutdown so Postgres does not keep dead backends."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def check_connection(engine: AsyncEngine) -> None:
    """Raise :class:`DatabaseUnavailableError` if Postgres is not usable.

    Called by the deep health check and at startup. We translate every driver
    exception into our own type so that no SQLAlchemy/asyncpg internals — which
    can include the connection string, and therefore the password — reach an
    HTTP response body.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, DBAPIError, OSError) as exc:
        raise DatabaseUnavailableError(
            detail=f"{type(exc).__name__}: cannot reach PostgreSQL"
        ) from exc


# --- Migrations --------------------------------------------------------------

_MIGRATION_FILENAME = re.compile(r"^(\d+)_[\w-]+\.sql$")


def _discover_migrations() -> list[tuple[int, Path]]:
    """Return ``(version, path)`` pairs sorted by version."""
    found: list[tuple[int, Path]] = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        match = _MIGRATION_FILENAME.match(path.name)
        if not match:
            log.warning("migration.skipped_bad_name", filename=path.name)
            continue
        found.append((int(match.group(1)), path))
    return sorted(found, key=lambda item: item[0])


async def _ensure_migration_table(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                filename    TEXT NOT NULL,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )


async def run_migrations(engine: AsyncEngine) -> list[int]:
    """Apply any migrations not yet recorded. Returns versions applied.

    Idempotent by design — safe to run on every boot, which is what lets
    ``docker compose up`` be genuinely one command. Each migration runs inside
    its own transaction: a failure halts the run with the database left at the
    last good version rather than half-migrated.
    """
    applied: list[int] = []

    try:
        async with engine.begin() as conn:
            await _ensure_migration_table(conn)
            result = await conn.execute(text("SELECT version FROM schema_migrations"))
            already = {row[0] for row in result.fetchall()}
    except (SQLAlchemyError, DBAPIError, OSError) as exc:
        raise DatabaseUnavailableError(
            detail=f"{type(exc).__name__} while reading schema_migrations"
        ) from exc

    for version, path in _discover_migrations():
        if version in already:
            continue

        sql = path.read_text(encoding="utf-8")
        with log_duration(log, "db.migration.apply", version=version, filename=path.name):
            try:
                async with engine.begin() as conn:
                    # A migration file holds many statements. asyncpg routes
                    # SQLAlchemy's text() through the extended-query protocol,
                    # which rejects multi-statement SQL ("cannot insert
                    # multiple commands into a prepared statement"). Dropping
                    # to the raw driver connection uses the simple-query
                    # protocol, which accepts a whole script. Splitting the
                    # file on ';' would be the alternative and is wrong — it
                    # breaks the $$ ... $$ function body below.
                    raw_connection = await conn.get_raw_connection()
                    await raw_connection.driver_connection.execute(sql)
                    await conn.execute(
                        text(
                            "INSERT INTO schema_migrations (version, filename) "
                            "VALUES (:version, :filename)"
                        ),
                        {"version": version, "filename": path.name},
                    )
            except (SQLAlchemyError, DBAPIError) as exc:
                raise DatabaseUnavailableError(
                    message="Database migration failed; the schema is not ready.",
                    detail=f"migration {path.name} failed: {type(exc).__name__}: {exc}",
                ) from exc
        applied.append(version)

    if applied:
        log.info("db.migrations.applied", versions=applied)
    else:
        log.info("db.migrations.up_to_date", known=len(already))
    return applied
