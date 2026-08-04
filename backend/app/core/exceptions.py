"""Exception hierarchy.

Every failure mode in the pipeline maps to exactly one exception class, and
each class carries the metadata needed to decide — without inspecting a
message string — whether to retry, alert, or drop the item.

Three attributes drive that decision automatically:

``retryable``
    The operation may succeed if attempted again (transient network, 503, lock
    contention). Celery retries these with exponential backoff.
``alerting``
    Something a human must look at: a selector broke, a portal changed, a
    circuit opened. Raises an alert, does not retry blindly.
``terminal``
    The item is definitively unusable (invalid file, duplicate). Recorded and
    dropped without retry and without alarm.

An exception is never allowed to escape a pipeline stage uncaught — the stage
converts it into a recorded outcome and the pipeline continues.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AuthenticationError",
    "BrowserActionError",
    "CircuitOpenError",
    "ConfigurationError",
    "ConnectorError",
    "CorruptedFileError",
    "CredentialsMissingError",
    "DownloadError",
    "DuplicateTenderError",
    "FileTooLargeError",
    "NormalizationError",
    "NotificationError",
    "ParsingError",
    "RateLimitedError",
    "RepositoryError",
    "RobotsDisallowedError",
    "SchedulingError",
    "ScoringError",
    "SelectorBrokenError",
    "SmartTenderError",
    "SourceUnavailableError",
    "StorageError",
    "SuspiciousContentError",
    "UnsupportedMediaTypeError",
    "ValidationError",
]


class SmartTenderError(Exception):
    """Root of the hierarchy. Never raised directly."""

    #: Stable machine-readable code surfaced in API responses and logs.
    code: str = "smarttender_error"
    #: HTTP status used when this bubbles up to the API layer.
    http_status: int = 500
    retryable: bool = False
    alerting: bool = False
    terminal: bool = False

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        #: Structured detail attached to logs and to the execution record.
        #: Must never contain credentials — see ``core.security.redact``.
        self.context: dict[str, Any] = context or {}
        self.cause = cause

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "alerting": self.alerting,
            "terminal": self.terminal,
            "context": self.context,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.context:
            detail = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
            return f"{self.message} ({detail})"
        return self.message


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class ConfigurationError(SmartTenderError):
    """Malformed or missing configuration. Fails fast at startup."""

    code = "configuration_error"
    http_status = 500
    alerting = True
    terminal = True


# ---------------------------------------------------------------------------
# Source / transport
# ---------------------------------------------------------------------------
class ConnectorError(SmartTenderError):
    """Base for anything that goes wrong inside a connector."""

    code = "connector_error"
    http_status = 502

    def __init__(
        self,
        message: str,
        *,
        connector: str | None = None,
        url: str | None = None,
        context: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        merged = dict(context or {})
        if connector:
            merged["connector"] = connector
        if url:
            merged["url"] = url
        super().__init__(message, context=merged, cause=cause)
        self.connector = connector
        self.url = url


class SourceUnavailableError(ConnectorError):
    """The portal is unreachable, timed out, or returned 5xx.

    Transient by assumption: retried with backoff, and alerted only once the
    circuit breaker opens for the source.
    """

    code = "source_unavailable"
    http_status = 503
    retryable = True


class RateLimitedError(ConnectorError):
    """The portal asked us to slow down (429, or our own bucket is empty)."""

    code = "rate_limited"
    http_status = 429
    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds
        if retry_after_seconds is not None:
            self.context["retry_after_seconds"] = retry_after_seconds


class CircuitOpenError(ConnectorError):
    """The circuit breaker is open — calls are refused without touching the network.

    Not retryable *now*: retrying is pointless until the recovery window
    elapses. The scheduler simply skips this source and runs the others.
    """

    code = "circuit_open"
    http_status = 503
    retryable = False
    alerting = True


class AuthenticationError(ConnectorError):
    """Login refused, session expired beyond recovery, or key rejected.

    Never retried: hammering a login endpoint with bad credentials gets the
    account locked.
    """

    code = "authentication_failed"
    http_status = 502
    retryable = False
    alerting = True
    terminal = True


class CredentialsMissingError(ConnectorError):
    """A paid/authenticated source has no credentials configured.

    Entirely expected (J360 without a subscription). The source is skipped
    quietly; no alert, no retry, and every other connector runs normally.
    """

    code = "credentials_missing"
    http_status = 400
    retryable = False
    alerting = False
    terminal = True


class RobotsDisallowedError(ConnectorError):
    """robots.txt forbids the path for our user agent."""

    code = "robots_disallowed"
    http_status = 403
    terminal = True


class DownloadError(ConnectorError):
    """The bytes could not be retrieved after every permitted attempt."""

    code = "download_failed"
    http_status = 502
    retryable = True


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class ParsingError(ConnectorError):
    """The document was retrieved but could not be interpreted."""

    code = "parsing_error"
    http_status = 422
    retryable = False
    alerting = True


class BrowserActionError(ConnectorError):
    """A required interaction with a rendered page could not be completed.

    Raised only for actions the result *depends* on — filling a search field,
    submitting a form. Optional interactions (dismissing a banner that was not
    shown) stay silent by design.

    The distinction matters because the failure is invisible otherwise: a
    search field that was never filled returns the portal's entire catalogue,
    and a crawl would report those unfiltered results as though they matched
    the user's criteria.
    """

    code = "browser_action_failed"
    retryable = True
    alerting = True


class SelectorBrokenError(ParsingError):
    """A guard selector matched nothing — the portal's markup changed.

    This is the single most valuable alert the platform emits: it is the
    difference between "this portal published nothing today" and "we have been
    silently blind to this portal for a week".
    """

    code = "selector_broken"
    alerting = True

    def __init__(self, message: str, *, selector: str | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.selector = selector
        if selector:
            self.context["selector"] = selector


class NormalizationError(ParsingError):
    """Fields were extracted but could not be coerced into the canonical model."""

    code = "normalization_error"
    alerting = False
    terminal = True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class ValidationError(SmartTenderError):
    """The input is rejected before entering the pipeline."""

    code = "validation_error"
    http_status = 422
    terminal = True

    def __init__(self, message: str, *, field: str | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.field = field
        if field:
            self.context["field"] = field


class FileTooLargeError(ValidationError):
    code = "file_too_large"
    http_status = 413


class UnsupportedMediaTypeError(ValidationError):
    code = "unsupported_media_type"
    http_status = 415


class CorruptedFileError(ValidationError):
    code = "corrupted_file"
    http_status = 422


class SuspiciousContentError(ValidationError):
    """Active content detected (macros, embedded JS, script tags)."""

    code = "suspicious_content"
    http_status = 422
    alerting = True


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
class DuplicateTenderError(SmartTenderError):
    """Already known. A completely normal, high-volume outcome — not an error
    in the operational sense, which is why it is terminal but never alerting."""

    code = "duplicate_tender"
    http_status = 409
    terminal = True

    def __init__(
        self,
        message: str,
        *,
        canonical_id: str | None = None,
        strategy: str | None = None,
        similarity: float | None = None,
        **kwargs: Any,
    ):
        super().__init__(message, **kwargs)
        self.canonical_id = canonical_id
        self.strategy = strategy
        self.similarity = similarity
        self.context.update(
            {
                k: v
                for k, v in (
                    ("canonical_id", canonical_id),
                    ("strategy", strategy),
                    ("similarity", similarity),
                )
                if v is not None
            }
        )


class StorageError(SmartTenderError):
    """Object storage refused or failed. Retryable — MinIO blips are common."""

    code = "storage_error"
    http_status = 503
    retryable = True
    alerting = True


class NotificationError(SmartTenderError):
    """Delivery failed. Retried, then dropped: a failed email must never
    prevent a tender from being ingested."""

    code = "notification_error"
    http_status = 502
    retryable = True


class SchedulingError(SmartTenderError):
    code = "scheduling_error"
    http_status = 400
    terminal = True


class ScoringError(SmartTenderError):
    """A scoring criterion blew up. The engine degrades to the remaining
    criteria rather than leaving the tender unscored."""

    code = "scoring_error"
    http_status = 500


class RepositoryError(SmartTenderError):
    code = "repository_error"
    http_status = 500
    retryable = True
