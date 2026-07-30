"""Writer for the queryable audit trail.

Deliberately separate from structlog. Log lines answer "what happened around
09:14?"; these rows answer "what happened to *this tender*?" months later,
after log retention has rolled over.

Writing an audit row must never be able to fail a pipeline stage — a failed
INSERT here is logged and swallowed, because losing an audit line is bad but
losing the tender it describes is worse.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import PipelineStage
from app.core.logging import current_context, get_logger
from app.core.security import redact, redact_url
from app.db.models.log import ExecutionLog

logger = get_logger(__name__)

__all__ = ["record_error", "record_event"]

_MAX_CONTEXT_CHARS = 8000


def _coerce_uuid(value: Any) -> uuid_module.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid_module.UUID):
        return value
    try:
        return uuid_module.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def record_event(
    session: Session,
    event: str,
    *,
    level: str = "INFO",
    stage: PipelineStage | str | None = None,
    connector: str | None = None,
    tender_id: Any = None,
    job_id: Any = None,
    run_id: Any = None,
    url: str | None = None,
    message: str | None = None,
    duration_ms: float | None = None,
    error_type: str | None = None,
    traceback: str | None = None,
    actor: str | None = None,
    context: dict[str, Any] | None = None,
) -> ExecutionLog | None:
    """Append one audit row. Returns ``None`` if it could not be written."""
    try:
        ambient = current_context()
        payload = redact(context or {})
        serialized = str(payload)
        if len(serialized) > _MAX_CONTEXT_CHARS:
            # Bound the row: a broken connector can produce enormous contexts,
            # and an audit table that fills the disk stops being an asset.
            payload = {
                "truncated": True,
                "preview": serialized[:1000],
                "original_size": len(serialized),
            }

        entry = ExecutionLog(
            level=level.upper(),
            event=event,
            stage=stage.value if isinstance(stage, PipelineStage) else stage,
            connector=connector or ambient.get("connector"),
            tender_id=_coerce_uuid(tender_id or ambient.get("tender_uuid")),
            job_id=_coerce_uuid(job_id or ambient.get("job_id")),
            run_id=_coerce_uuid(run_id),
            task_id=ambient.get("task_id"),
            correlation_id=ambient.get("correlation_id") or ambient.get("request_id"),
            actor=actor,
            url=redact_url(url),
            message=(message or "")[:4000] or None,
            duration_ms=duration_ms,
            error_type=error_type,
            traceback=(traceback or "")[:8000] or None,
            context=payload,
        )
        session.add(entry)
        return entry
    except Exception as exc:
        logger.warning("audit.write_failed", event=event, error=str(exc))
        return None


def record_error(
    session: Session,
    event: str,
    exc: BaseException,
    **kwargs: Any,
) -> ExecutionLog | None:
    """Convenience wrapper that fills the error fields from an exception."""
    import traceback as traceback_module

    from app.core.exceptions import SmartTenderError

    context = dict(kwargs.pop("context", None) or {})
    if isinstance(exc, SmartTenderError):
        context.update(exc.context)
        message = exc.message
    else:
        message = str(exc)

    return record_event(
        session,
        event,
        level=kwargs.pop("level", "ERROR"),
        message=message,
        error_type=type(exc).__name__,
        traceback="".join(
            traceback_module.format_exception(type(exc), exc, exc.__traceback__)
        ),
        context=context,
        **kwargs,
    )
