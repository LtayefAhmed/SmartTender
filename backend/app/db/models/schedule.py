"""Database-backed schedules for Celery Beat.

Celery's stock scheduler reads a Python dict frozen at process start, so
changing a schedule means editing code and restarting Beat. That is
unacceptable for a product where operators tune cadence per source.

These rows *are* the schedule. A custom scheduler (see
``app.workers.beat.DatabaseScheduler``) reloads them whenever
``ScheduleChangeSentinel.last_update`` moves, so an edit made through the API
takes effect within one sync interval with nothing restarted.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ScheduleKind
from app.db.base import Base, JSONType, StringArray, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.job import ScrapingJob


class Schedule(Base, TimestampMixin):
    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'interval' AND interval_seconds IS NOT NULL) OR "
            "(kind = 'crontab' AND cron_minute IS NOT NULL)",
            name="kind_requires_matching_fields",
        ),
        Index("ix_schedules_enabled_next_run", "enabled", "next_run_at"),
        {"comment": "Operator-editable Celery Beat entries."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ScheduleKind.INTERVAL.value
    )

    # --- interval form -----------------------------------------------------
    #: Presets in the UI (hourly, every 2h, every 6h, daily, weekly) all resolve
    #: to a number of seconds; arbitrary values are equally valid.
    interval_seconds: Mapped[int | None] = mapped_column(Integer)

    # --- crontab form ------------------------------------------------------
    cron_minute: Mapped[str | None] = mapped_column(String(64))
    cron_hour: Mapped[str | None] = mapped_column(String(64))
    cron_day_of_week: Mapped[str | None] = mapped_column(String(64))
    cron_day_of_month: Mapped[str | None] = mapped_column(String(64))
    cron_month_of_year: Mapped[str | None] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Africa/Tunis")

    # --- payload -----------------------------------------------------------
    #: Empty means "every enabled source".
    connectors: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    task_name: Mapped[str] = mapped_column(
        String(255), nullable=False, default="app.workers.tasks.scraping.run_scraping_job"
    )
    queue: Mapped[str | None] = mapped_column(String(64))
    #: Discard a due run older than this instead of stampeding after downtime —
    #: Beat catching up on 40 missed hourly runs at once would be worse than
    #: skipping them.
    expire_seconds: Mapped[int | None] = mapped_column(Integer, default=3600)
    #: Refuse to start if the previous run of this schedule is still going.
    skip_if_running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- bookkeeping (written by Beat) -------------------------------------
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    total_run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_job_id: Mapped[uuid_module.UUID | None] = mapped_column(Uuid(as_uuid=True))

    #: One-shot schedules disable themselves after firing.
    one_off: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    start_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[str | None] = mapped_column(String(128))
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    jobs: Mapped[list[ScrapingJob]] = relationship(back_populates="schedule", lazy="noload")

    def describe(self) -> str:
        """Human-readable cadence, for the API and the admin console."""
        if self.kind == ScheduleKind.INTERVAL.value and self.interval_seconds:
            seconds = self.interval_seconds
            if seconds % 86400 == 0:
                n = seconds // 86400
                return "every day" if n == 1 else f"every {n} days"
            if seconds % 3600 == 0:
                n = seconds // 3600
                return "every hour" if n == 1 else f"every {n} hours"
            if seconds % 60 == 0:
                n = seconds // 60
                return "every minute" if n == 1 else f"every {n} minutes"
            return f"every {seconds} seconds"
        return (
            f"cron({self.cron_minute or '*'} {self.cron_hour or '*'} "
            f"{self.cron_day_of_month or '*'} {self.cron_month_of_year or '*'} "
            f"{self.cron_day_of_week or '*'}) {self.timezone}"
        )


class ScheduleChangeSentinel(Base):
    """Single-row table whose timestamp tells Beat that schedules changed.

    Beat polls this one cheap row instead of re-reading every schedule on every
    tick, so the sync cost is constant regardless of how many schedules exist.
    """

    __tablename__ = "schedule_change_sentinel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_update: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
