"""Structured logging.

``print()`` is banned in this codebase. Every log line is a structured event
with a stable name plus key/value pairs, rendered as JSON in production and as
readable columns in development.

Ambient context — request id, tender uuid, connector, job id — is bound to
context variables so it is attached to every subsequent line automatically. A
developer reading production logs can therefore take any tender UUID and
reconstruct its complete journey through the pipeline with one query.
"""

from __future__ import annotations

import logging
import logging.config
import sys
import time
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.config import get_settings

# ---------------------------------------------------------------------------
# Ambient correlation context
# ---------------------------------------------------------------------------
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_tender_uuid: ContextVar[str | None] = ContextVar("tender_uuid", default=None)
_connector: ContextVar[str | None] = ContextVar("connector", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)

_CONTEXT_VARS: dict[str, ContextVar[str | None]] = {
    "request_id": _request_id,
    "correlation_id": _correlation_id,
    "tender_uuid": _tender_uuid,
    "connector": _connector,
    "job_id": _job_id,
    "task_id": _task_id,
}

#: Keys whose values are replaced by a placeholder before a line is emitted.
#: Substring match, case-insensitive — errs on the side of over-redacting.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "cookie",
        "set-cookie",
        "session",
        "access_key",
        "secret_key",
        "private_key",
        "x-api-key",
        "username",
        "login",
    }
)

REDACTED = "***redacted***"


def is_sensitive_key(key: str) -> bool:
    return any(marker in key.lower() for marker in SENSITIVE_KEYS)


def bind_context(**values: str | None) -> None:
    """Bind ambient values for the remainder of this task/request."""
    for key, value in values.items():
        var = _CONTEXT_VARS.get(key)
        if var is not None:
            var.set(value)


def clear_context() -> None:
    for var in _CONTEXT_VARS.values():
        var.set(None)


def current_context() -> dict[str, str]:
    return {k: v for k, v in ((k, var.get()) for k, var in _CONTEXT_VARS.items()) if v}


@contextmanager
def log_context(**values: str | None) -> Iterator[None]:
    """Temporarily bind context, restoring the previous values on exit."""
    tokens = []
    for key, value in values.items():
        var = _CONTEXT_VARS.get(key)
        if var is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# structlog processors
# ---------------------------------------------------------------------------
def _inject_context(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in current_context().items():
        event_dict.setdefault(key, value)
    return event_dict


def _inject_service(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    settings = get_settings()
    event_dict.setdefault("service", settings.service_name)
    event_dict.setdefault("env", settings.env)
    return event_dict


def _redact(value: Any, key: str = "") -> Any:
    # Containers are always recursed into, even when their own key looks
    # sensitive: blanket-masking a whole ``auth`` block would hide the mode,
    # the endpoint and the error that make a log line useful, while the leaves
    # underneath still get masked individually.
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(v, key) for v in value)
    if is_sensitive_key(key):
        return REDACTED
    return value


def _redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {key: _redact(value, key) for key, value in event_dict.items()}


def _rename_event(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
_configured = False


def configure_logging(force: bool = False) -> None:
    """Configure structlog and the stdlib root logger. Idempotent."""
    global _configured
    if _configured and not force:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level, logging.INFO)
    json_output = settings.log_format.lower() == "json"

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _inject_context,
        _inject_service,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _redact_processor,
    ]

    if json_output:
        renderer: Any = structlog.processors.JSONRenderer()
        final: list[Any] = [
            structlog.processors.format_exc_info,
            _rename_event,
            renderer,
        ]
    else:
        final = [
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty(), exception_formatter=
                                          structlog.dev.plain_traceback),
        ]

    structlog.configure(
        processors=[*shared, *final],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Emit through the stdlib logging tree rather than writing to stdout
        # directly: uvicorn, celery and SQLAlchemy already log there, and a
        # single handler is the only way to get one consistent format in
        # production. It is also what gives every logger a real `.name`.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib loggers (uvicorn, sqlalchemy, celery, httpx) through the same
    # handler so there is exactly one log format in production.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    for noisy, noisy_level in (
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("urllib3", logging.WARNING),
        ("botocore", logging.WARNING),
        ("sqlalchemy.engine", logging.WARNING),
        ("asyncio", logging.WARNING),
        ("multipart", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(noisy_level)

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structured logger. Configures logging on first use."""
    configure_logging()
    return structlog.get_logger(name)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------
@contextmanager
def timed(
    logger: structlog.stdlib.BoundLogger,
    event: str,
    *,
    level: str = "info",
    **fields: Any,
) -> Iterator[dict[str, Any]]:
    """Log an event with its duration, whether it succeeds or raises.

    The yielded dict can be mutated to attach results discovered during the
    block (item counts, ids), which are then included in the final line.
    """
    extra: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        yield extra
    except BaseException as exc:
        duration = time.perf_counter() - started
        logger.error(
            event,
            outcome="error",
            duration_seconds=round(duration, 4),
            error_type=type(exc).__name__,
            error=str(exc),
            **fields,
            **extra,
        )
        raise
    else:
        duration = time.perf_counter() - started
        getattr(logger, level)(
            event,
            outcome="success",
            duration_seconds=round(duration, 4),
            **fields,
            **extra,
        )


__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "bind_context",
    "clear_context",
    "configure_logging",
    "current_context",
    "get_logger",
    "log_context",
    "new_request_id",
    "timed",
]
