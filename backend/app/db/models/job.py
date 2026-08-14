"""Scraping jobs and per-connector runs.

The split is what implements the isolation invariant. A ``ScrapingJob`` is the
user's request ("search these sources with these filters"); a ``ConnectorRun``
is one connector's independent attempt at it. Runs never depend on one another,
and the job's status is *derived* from its runs — which is why a job with three
successes and one failure is ``PARTIAL``, not ``FAILED``.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import JobStatus, JobTrigger
from app.db.base import Base, JSONType, StringArray, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.schedule import Schedule
    from app.db.models.source import Source


class ScrapingJob(Base, TimestampMixin):
    __tablename__ = "scraping_jobs"
    __table_args__ = (
        Index("ix_scraping_jobs_status_created", "status", "created_at"),
        Index("ix_scraping_jobs_trigger_created", "trigger", "created_at"),
        {"comment": "One user- or schedule-initiated scraping request."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )

    trigger: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobTrigger.MANUAL.value, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobStatus.PENDING.value, index=True
    )

    #: Connector keys requested. Empty means "every enabled source".
    requested_connectors: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )
    #: Normalised search filters as submitted by the UI, kept verbatim so a run
    #: can be replayed identically.
    filters: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    requested_by: Mapped[str | None] = mapped_column(String(128), index=True)
    schedule_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("schedules.id", ondelete="SET NULL"), index=True
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(128), index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    # --- aggregate counters, maintained by the runs -----------------------
    connectors_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    connectors_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    connectors_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    connectors_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_ingested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: One entry per failed connector: ``{connector, error_type, message}``.
    #: A partial failure is data, not an exception.
    errors: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    runs: Mapped[list[ConnectorRun]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )
    schedule: Mapped[Schedule | None] = relationship(back_populates="jobs", lazy="noload")

    @property
    def _counters(self) -> tuple[int, int, int, int]:
        """Counters as integers.

        Column defaults are only materialised on flush, so a job assembled in
        memory (or read back before a refresh) legitimately has ``None`` here.
        Arithmetic on those must not raise.
        """
        return (
            int(self.connectors_total or 0),
            int(self.connectors_succeeded or 0),
            int(self.connectors_failed or 0),
            int(self.connectors_skipped or 0),
        )

    @property
    def progress(self) -> float:
        """Fraction of requested connectors that have reached a terminal state."""
        total, succeeded, failed, skipped = self._counters
        if not total:
            return 0.0
        return round(min((succeeded + failed + skipped) / total, 1.0), 4)

    def derive_status(self) -> str:
        """Compute the job status from its connector outcomes.

        A job only *fails* when every connector failed. As long as one source
        produced something, the run is a partial success and the pipeline moved
        forward — that is the whole point of connector isolation.
        """
        total, succeeded, failed, skipped = self._counters
        if succeeded + failed + skipped < total:
            return JobStatus.RUNNING.value
        if total == 0 or failed == 0:
            return JobStatus.SUCCEEDED.value
        if succeeded == 0 and skipped == 0:
            return JobStatus.FAILED.value
        return JobStatus.PARTIAL.value


class ConnectorRun(Base, TimestampMixin):
    __tablename__ = "connector_runs"
    __table_args__ = (
        Index("ix_connector_runs_source_created", "source_id", "created_at"),
        Index("ix_connector_runs_status", "status"),
        {"comment": "One connector's isolated attempt within a scraping job."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    job_id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("scraping_jobs.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sources.id", ondelete="SET NULL")
    )
    #: Denormalised on purpose: a run's history must remain readable even if the
    #: source row is later removed.
    connector_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobStatus.PENDING.value
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(128), index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    pages_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_downloaded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    items_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_ingested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    error_type: Mapped[str | None] = mapped_column(String(64), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    error_context: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    #: Per-item failures that did not abort the run. Bounded when written.
    item_errors: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    filters: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    job: Mapped[ScrapingJob] = relationship(back_populates="runs", lazy="noload")
    source: Mapped[Source | None] = relationship(back_populates="runs", lazy="noload")
