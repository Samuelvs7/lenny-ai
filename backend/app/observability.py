"""Structured logging and request correlation.

Design goals, taken from the operability requirements in the brief:

* One line per event, machine-parseable in production (``LOG_FORMAT=json``)
  and readable during development (``LOG_FORMAT=console``).
* Every log line inside a request carries the same ``request_id``, so a single
  user-visible failure can be traced across routing, retrieval, the model call
  and the database without guessing.
* Model, retrieval and database operations emit timing, because "it's slow" is
  the most common operational complaint about a RAG system and you cannot act
  on it without knowing which stage was slow.
* Never log secrets or full prompt/response bodies. We log shapes and counts
  (token estimates, chunk counts, latency), not content.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

from app.config import LogFormat, Settings

#: Correlation id for the in-flight request. Set by the middleware, read by
#: every logger and echoed back to the client in the ``X-Request-ID`` header.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

#: Session the request belongs to, when known. Lets you grep one conversation.
session_id_var: ContextVar[str] = ContextVar("session_id", default="-")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def _inject_context(_logger: Any, _name: str, event_dict: dict) -> dict:
    """Attach correlation ids to every event."""
    event_dict["request_id"] = request_id_var.get()
    session_id = session_id_var.get()
    if session_id != "-":
        event_dict["session_id"] = session_id
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Install structlog + stdlib logging. Safe to call more than once."""
    level = getattr(logging, settings.log_level, logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        # NOTE: structlog.stdlib.add_logger_name is deliberately absent. It
        # reads ``logger.name``, which PrintLogger does not have, and raises
        # AttributeError from inside the logging call — turning any log line
        # into a crash. The logger name is bound explicitly by get_logger().
        _inject_context,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_format is LogFormat.JSON:
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib loggers (uvicorn, sqlalchemy, httpx) through the same sink so
    # operators have exactly one log format to parse.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger tagged with its module name."""
    return structlog.get_logger().bind(logger=name)


@contextmanager
def log_duration(
    logger: structlog.stdlib.BoundLogger,
    event: str,
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    """Time a block and emit one event with ``duration_ms``.

    Yields a mutable dict so the caller can enrich the final log line with
    results that are only known once the work is done::

        with log_duration(log, "retrieval.search", query_words=7) as span:
            hits = search(...)
            span["hit_count"] = len(hits)

    On exception the event is logged at ``error`` with the elapsed time, then
    the exception propagates — timing data is never lost to a failure.
    """
    started = time.perf_counter()
    span: dict[str, Any] = dict(fields)
    try:
        yield span
    except Exception as exc:
        span["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        span["outcome"] = "error"
        span["error_type"] = type(exc).__name__
        logger.error(event, **span)
        raise
    else:
        span["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        span.setdefault("outcome", "ok")
        logger.info(event, **span)
