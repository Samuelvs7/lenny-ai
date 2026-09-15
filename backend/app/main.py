"""FastAPI application: startup, middleware, error handling.

Startup is deliberately *fault-tolerant*. If Postgres is unreachable or the
knowledge base is empty, the app still starts and serves ``/health/deep`` with
the reason. A process that crash-loops on a missing dependency hides the very
information an operator needs, and under Docker Compose it races the database
container on first boot.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import build_state
from app.api.routes import APP_VERSION, health_router, router
from app.config import Settings, get_settings
from app.db.engine import dispose_engine, get_engine, run_migrations
from app.errors import AppError, InvalidRequestError
from app.observability import (
    configure_logging,
    get_logger,
    log_duration,
    new_request_id,
    request_id_var,
    session_id_var,
)

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = get_settings()
    configure_logging(settings)
    log.info("app.starting", version=APP_VERSION, **settings.describe_safe())

    engine = get_engine(settings)
    state = build_state(settings, engine)
    app.state.app_state = state

    try:
        await run_migrations(engine)
    except Exception as exc:
        state.startup_error = (
            f"Database unavailable at startup: {type(exc).__name__}. "
            "The API is running but cannot persist conversations. "
            "Check DATABASE_URL and that PostgreSQL is reachable."
        )
        log.error("app.startup_database_failed", detail=state.startup_error)

    if state.startup_error is None:
        try:
            with log_duration(log, "app.index_warm") as span:
                span["chunks"] = await state.dense_index.load(engine)
            if state.dense_index.size == 0:
                log.warning(
                    "app.knowledge_base_empty",
                    action="run `python -m app.ingestion` to index transcripts",
                )
        except Exception as exc:
            log.error("app.index_load_failed", error=f"{type(exc).__name__}: {exc}")

    log.info("app.ready", dense_index_chunks=state.dense_index.size)
    try:
        yield
    finally:
        log.info("app.shutting_down")
        await state.providers.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="The Lenny Growth Assistant",
        description=(
            "A grounded assistant over Lenny's Podcast transcripts: cited answers, "
            "Ship 30 for 30 essays, and rendered Markdown/HTML artifacts."
        ),
        version=APP_VERSION,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Tag every request with a correlation id and log its outcome."""
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        request_id_var.set(request_id)
        session_id_var.set("-")

        with log_duration(
            log,
            "http.request",
            method=request.method,
            path=request.url.path,
        ) as span:
            response = await call_next(request)
            span["status_code"] = response.status_code
            if response.status_code >= 500:
                span["outcome"] = "error"

        response.headers["X-Request-ID"] = request_id
        return response

    _install_error_handlers(app, settings)

    app.include_router(health_router)
    app.include_router(router, prefix="/api")
    return app


def _install_error_handlers(app: FastAPI, settings: Settings) -> None:
    """Map exceptions onto the structured error contract.

    ``detail`` (developer context) is included outside production only. In
    production it stays in the logs, reachable by request id — the client gets
    a clear message and a correlation id, never internals.
    """
    include_detail = settings.app_env.lower() != "production"

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        request_id = request_id_var.get()
        log.warning(
            "error.handled",
            code=exc.code,
            status=exc.http_status,
            detail=exc.detail,
            **exc.context,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_payload(request_id=request_id, include_detail=include_detail),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Flatten pydantic's nested errors into one readable sentence, so the
        # UI can show something useful without parsing the error tree.
        problems = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", []) if part != "body")
            problems.append(f"{location or 'request'}: {error.get('msg', 'invalid')}")
        message = "; ".join(problems) or "The request was not valid."

        wrapped = InvalidRequestError(message, detail=str(exc.errors())[:500])
        request_id = request_id_var.get()
        log.info("error.validation", problems=problems)
        return JSONResponse(
            status_code=wrapped.http_status,
            content=wrapped.to_payload(request_id=request_id, include_detail=include_detail),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        """Last resort. Never leaks the exception text to the client."""
        request_id = request_id_var.get()
        log.error(
            "error.unhandled",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong on our side.",
                    "retryable": True,
                    "request_id": request_id,
                }
            },
            headers={"X-Request-ID": request_id},
        )


app = create_app()
