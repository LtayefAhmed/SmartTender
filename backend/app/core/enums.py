"""Canonical domain vocabulary.

Every portal has its own words for the same concepts. Connectors translate into
these enums during normalisation, so the rest of the platform — scoring,
filtering, the dashboard — only ever sees one vocabulary.

Values are stored in PostgreSQL as strings (not native enums) so that adding a
member never requires a migration and an unknown value from a portal degrades
to ``UNKNOWN`` instead of crashing an insert.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "CircuitState",
    "DuplicateStrategy",
    "EntryPoint",
    "FetchStrategy",
    "JobStatus",
    "JobTrigger",
    "NotificationChannel",
    "NotificationStatus",
    "PipelineStage",
    "ProcurementType",
    "RelevanceBand",
    "ScheduleKind",
    "SourceHealth",
    "SubmissionOutcome",
    "SubmissionStatus",
    "TenderPipelineState",
    "TenderStatus",
    "coerce",
]


class _StrEnum(str, Enum):
    """String enum with a tolerant parser (``str`` base keeps Python 3.10 support)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


def coerce(enum_cls: type[_StrEnum], value: object, default: _StrEnum) -> _StrEnum:
    """Best-effort conversion of external input into an enum member.

    Never raises: an unrecognised value from a third-party portal becomes the
    supplied default. Losing one field's precision is always preferable to
    aborting the ingestion of an otherwise valid tender.
    """
    if isinstance(value, enum_cls):
        return value
    if value is None:
        return default
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return default
    for member in enum_cls:
        if member.value.lower() == text or member.name.lower() == text:
            return member
    return default


class EntryPoint(_StrEnum):
    """How a tender entered the platform."""

    MANUAL_UPLOAD = "manual_upload"
    MANUAL_SCRAPE = "manual_scrape"
    SCHEDULED_SCRAPE = "scheduled_scrape"
    API = "api"


class TenderStatus(_StrEnum):
    """Lifecycle of the opportunity at the buyer's side."""

    OPEN = "open"
    CLOSED = "closed"
    AWARDED = "awarded"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ProcurementType(_StrEnum):
    OPEN = "open"
    RESTRICTED = "restricted"
    NEGOTIATED = "negotiated"
    FRAMEWORK_AGREEMENT = "framework_agreement"
    PRIOR_INFORMATION_NOTICE = "prior_information_notice"
    EXPRESSION_OF_INTEREST = "expression_of_interest"
    DIRECT_AWARD = "direct_award"
    UNKNOWN = "unknown"


class PipelineStage(_StrEnum):
    """Named stages, used for metrics labels and execution logs."""

    FETCH = "fetch"
    PARSE = "parse"
    VALIDATE = "validate"
    DEDUPLICATE = "deduplicate"
    STORE = "store"
    INGEST = "ingest"
    EXTRACT = "extract"
    SCORE = "score"
    NOTIFY = "notify"


class TenderPipelineState(_StrEnum):
    """Where a persisted tender currently sits.

    ``RECEIVED`` is written before any heavy work starts, which is what lets the
    HTTP request return immediately.
    """

    RECEIVED = "received"
    QUEUED = "queued"
    PARSING = "parsing"
    PARSED = "parsed"
    SCORING = "scoring"
    SCORED = "scored"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class RelevanceBand(_StrEnum):
    HIGHLY_RELEVANT = "highly_relevant"
    RELEVANT = "relevant"
    LOW_RELEVANCE = "low_relevance"
    OUT_OF_SCOPE = "out_of_scope"
    UNSCORED = "unscored"


class JobStatus(_StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    #: At least one connector failed while others succeeded — the normal
    #: outcome of a multi-source run and explicitly *not* a job failure.
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class JobTrigger(_StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    API = "api"
    RETRY = "retry"


class SourceHealth(_StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILING = "failing"
    DISABLED = "disabled"
    CREDENTIALS_MISSING = "credentials_missing"
    UNKNOWN = "unknown"


class CircuitState(_StrEnum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


class DuplicateStrategy(_StrEnum):
    CANONICAL_URL = "canonical_url"
    RAW_HASH = "raw_hash"
    TEXT_HASH = "text_hash"
    SEMANTIC = "semantic"
    EXTERNAL_ID = "external_id"


class NotificationChannel(_StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"


class NotificationStatus(_StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"
    READ = "read"


class ScheduleKind(_StrEnum):
    INTERVAL = "interval"
    CRONTAB = "crontab"


class SubmissionStatus(_StrEnum):
    """Where our response to a tender currently stands."""

    DRAFT = "draft"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    WITHDRAWN = "withdrawn"
    #: We looked at it and chose not to bid — a decision worth recording, since
    #: it is as informative as a loss when tuning relevance.
    DECLINED = "declined"


class SubmissionOutcome(_StrEnum):
    """How a submitted bid turned out.

    ``PENDING`` is the normal state for months: public awards are published
    long after the deadline. Only decided outcomes feed the win rate.
    """

    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    CANCELLED = "cancelled"
    NOT_SUBMITTED = "not_submitted"


class FetchStrategy(_StrEnum):
    STATIC = "static"
    DYNAMIC = "dynamic"
    API = "api"
    FIXTURE = "fixture"
