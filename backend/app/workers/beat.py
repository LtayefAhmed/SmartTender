"""Database-backed Celery Beat scheduler.

Celery's stock scheduler reads a Python dict frozen at process start. Changing
a cadence therefore means editing code and restarting Beat — unacceptable for a
product whose operators need to tune per-source frequency from the admin
console.

This scheduler treats the ``schedules`` table as the source of truth. It polls
a single-row sentinel and reloads only when that timestamp moves, so the cost
of "check for changes" is one trivial query per sync interval regardless of how
many schedules exist. An edit made through the API takes effect within one
interval, with nothing restarted.

Two protections that matter in production:

**Expiry.** After downtime, Beat would otherwise fire every missed run at once.
``expire_seconds`` discards runs that are already stale — a stampede of forty
catch-up scrapes is worse than skipping them.

**Overlap.** ``skip_if_running`` refuses to start a schedule whose previous job
is still going, so a portal that got slow cannot accumulate concurrent runs
until it collapses.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from celery import schedules as celery_schedules
from celery.beat import ScheduleEntry, Scheduler
from sqlalchemy import select

from app.core.enums import JobStatus, ScheduleKind
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.db.models.job import ScrapingJob
from app.db.models.schedule import Schedule, ScheduleChangeSentinel
from app.db.session import session_scope

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_SCRAPING_TASK",
    "DatabaseScheduler",
    "ModelEntry",
    "touch_schedule_sentinel",
]

#: Task a schedule fires unless it names another one.
DEFAULT_SCRAPING_TASK = "app.workers.tasks.scraping.run_scraping_job"


def touch_schedule_sentinel(session: Any) -> None:
    """Signal Beat that the schedule table changed.

    Called by the API after any schedule create/update/delete. Beat notices on
    its next sync, which is why an edit does not require a restart.
    """
    sentinel = session.get(ScheduleChangeSentinel, 1)
    if sentinel is None:
        session.add(ScheduleChangeSentinel(id=1, last_update=utc_now()))
    else:
        sentinel.last_update = utc_now()


def _to_celery_schedule(row: Schedule) -> celery_schedules.BaseSchedule:
    if row.kind == ScheduleKind.CRONTAB.value:
        return celery_schedules.crontab(
            minute=row.cron_minute or "*",
            hour=row.cron_hour or "*",
            day_of_week=row.cron_day_of_week or "*",
            day_of_month=row.cron_day_of_month or "*",
            month_of_year=row.cron_month_of_year or "*",
        )
    return celery_schedules.schedule(
        run_every=timedelta(seconds=int(row.interval_seconds or 3600))
    )


class ModelEntry(ScheduleEntry):
    """A Beat entry backed either by a database row or by static config.

    Both forms are required. Operator-editable schedules come from the
    ``schedules`` table, while the maintenance loops are pinned in
    ``beat_schedule`` precisely so an operator *cannot* switch off the
    reconciliation that keeps the pipeline honest. Celery instantiates static
    entries as ``Entry(name=..., task=..., schedule=...)``, so this class has to
    accept that shape as well as a model — otherwise the scheduler cannot host
    its own housekeeping and crashes on startup.
    """

    def __init__(self, model: Schedule | None = None, app: Any = None, **kwargs: Any) -> None:
        if model is None:
            # Static entry from `beat_schedule`. No database bookkeeping: it is
            # defined in code and has nothing to write back.
            self.model = None
            self.schedule_id = None
            super().__init__(app=app, **kwargs)
            return

        self.model = model
        self.schedule_id = model.id
        self.app = app

        super().__init__(
            name=model.name,
            # Column defaults only materialise on flush, so a row assembled in
            # memory can still have `task_name` unset. Publishing a message
            # with no task name is silently unroutable, which is the worst
            # possible failure for a scheduler.
            task=model.task_name or DEFAULT_SCRAPING_TASK,
            schedule=_to_celery_schedule(model),
            args=(),
            kwargs={
                "schedule_id": str(model.id),
                "connectors": list(model.connectors or []),
                "filters": dict(model.filters or {}),
                "trigger": "scheduled",
            },
            options={
                "queue": model.queue or "scraping",
                "expires": model.expire_seconds or None,
            },
            last_run_at=model.last_run_at or utc_now(),
            total_run_count=model.total_run_count or 0,
            app=app,
        )

    # ------------------------------------------------------------------
    def is_due(self) -> tuple[bool, float]:
        """Decide whether to fire, with the product's own guard rails first."""
        if self.model is None:
            # Static maintenance entry: plain interval/crontab semantics.
            return super().is_due()

        if not self.model.enabled:
            return False, 300.0

        now = utc_now()
        if self.model.start_after and now < self.model.start_after:
            delay = (self.model.start_after - now).total_seconds()
            return False, min(max(delay, 5.0), 300.0)

        if self.model.expires_at and now >= self.model.expires_at:
            logger.info("schedule.expired", name=self.model.name)
            return False, 3600.0

        due, next_check = super().is_due()
        if not due:
            return due, next_check

        if self.model.skip_if_running and self._previous_run_active():
            logger.warning(
                "schedule.skipped.previous_run_active",
                name=self.model.name,
                last_job_id=str(self.model.last_job_id) if self.model.last_job_id else None,
            )
            # Re-check soon rather than waiting a full period: the moment the
            # running job finishes we want to be back on cadence.
            return False, min(next_check, 60.0)

        return True, next_check

    def _previous_run_active(self) -> bool:
        if not self.model.last_job_id:
            return False
        try:
            with session_scope() as session:
                status = session.execute(
                    select(ScrapingJob.status).where(ScrapingJob.id == self.model.last_job_id)
                ).scalar_one_or_none()
        except Exception as exc:
            # If we cannot tell, allow the run. A missed scrape is worse than
            # an overlapping one, and the job itself is idempotent.
            logger.warning("schedule.overlap_check_failed", error=str(exc))
            return False
        return status in {JobStatus.PENDING.value, JobStatus.RUNNING.value}

    def __next__(self) -> ModelEntry:
        """Advance after firing. Persisted by the scheduler's ``sync``."""
        if self.model is None:
            # Static entry: nothing to write back, just advance in memory.
            return self.__class__(
                app=self.app,
                name=self.name,
                task=self.task,
                schedule=self.schedule,
                args=self.args,
                kwargs=self.kwargs,
                options=self.options,
                last_run_at=utc_now(),
                total_run_count=(self.total_run_count or 0) + 1,
            )

        self.model.last_run_at = utc_now()
        self.model.total_run_count = (self.model.total_run_count or 0) + 1
        if self.model.one_off:
            self.model.enabled = False
            logger.info("schedule.one_off_completed", name=self.model.name)

        entry = self.__class__(self.model, app=self.app)
        entry.last_run_at = self.model.last_run_at
        entry.total_run_count = self.model.total_run_count
        return entry

    next = __next__


class DatabaseScheduler(Scheduler):
    """Beat scheduler whose entries come from PostgreSQL."""

    Entry = ModelEntry

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._schedule: dict[str, ModelEntry] = {}
        #: Entries pinned in `beat_schedule` (the maintenance loops). Held
        #: separately because a database reload replaces `_schedule` wholesale
        #: and would otherwise silently drop them — leaving the pipeline with
        #: no reconciliation at all after the first schedule edit.
        self._static: dict[str, ModelEntry] = {}
        self._last_sentinel: datetime | None = None
        self._last_db_check: datetime | None = None
        self._dirty: set[str] = set()
        self._lock = threading.RLock()

        from app.core.config import get_settings

        self._sync_interval = get_settings().worker.beat_sync_interval_seconds
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------
    def setup_schedule(self) -> None:
        self._reload()
        # Merge in the entries pinned in ``beat_schedule`` — the housekeeping
        # loops, which are infrastructure rather than policy and deliberately
        # not operator-editable.
        self.install_default_entries(self._schedule)
        self.update_from_dict(self.app.conf.beat_schedule or {})
        with self._lock:
            self._static = {
                name: entry
                for name, entry in self._schedule.items()
                if getattr(entry, "model", None) is None
            }
        logger.info(
            "beat.schedule_ready",
            database_entries=len(self._schedule) - len(self._static),
            static_entries=len(self._static),
        )

    def _sentinel_changed(self) -> bool:
        try:
            with session_scope() as session:
                sentinel = session.get(ScheduleChangeSentinel, 1)
                current = sentinel.last_update if sentinel else None
        except Exception as exc:
            logger.warning("beat.sentinel_check_failed", error=str(exc))
            return False
        if current != self._last_sentinel:
            self._last_sentinel = current
            return True
        return False

    def _reload(self) -> None:
        try:
            with session_scope() as session:
                rows = list(
                    session.execute(select(Schedule).where(Schedule.enabled.is_(True)))
                    .scalars()
                    .all()
                )
                # Detach so the entries survive the closed session.
                for row in rows:
                    session.expunge(row)
                sentinel = session.get(ScheduleChangeSentinel, 1)
                self._last_sentinel = sentinel.last_update if sentinel else None
        except Exception as exc:
            # Keep serving the previous schedule rather than going silent.
            logger.error("beat.reload_failed.keeping_previous", error=str(exc))
            return

        with self._lock:
            # Static entries survive every reload; database entries win on a
            # name clash, so an operator can override a pinned cadence.
            self._schedule = {
                **self._static,
                **{row.name: ModelEntry(row, app=self.app) for row in rows},
            }
            self._last_db_check = utc_now()
        logger.info(
            "beat.schedule_loaded",
            database_entries=len(rows),
            static_entries=len(self._static),
        )

    @property
    def schedule(self) -> dict[str, ModelEntry]:
        stale = (
            self._last_db_check is None
            or (utc_now() - self._last_db_check).total_seconds() >= self._sync_interval
        )
        if stale and self._sentinel_changed():
            logger.info("beat.change_detected.reloading")
            self._reload()
        elif stale:
            with self._lock:
                self._last_db_check = utc_now()
        return self._schedule

    # ------------------------------------------------------------------
    def apply_entry(self, entry: ScheduleEntry, producer: Any = None) -> None:
        # Only database-backed entries have run bookkeeping to persist.
        if isinstance(entry, ModelEntry) and entry.model is not None:
            self._dirty.add(entry.name)
        super().apply_entry(entry, producer=producer)

    def sync(self) -> None:
        """Persist run bookkeeping for the entries that fired."""
        with self._lock:
            dirty = list(self._dirty)
            self._dirty.clear()
        if not dirty:
            return

        try:
            with session_scope() as session:
                for name in dirty:
                    entry = self._schedule.get(name)
                    if entry is None or entry.model is None:
                        continue
                    row = session.get(Schedule, entry.schedule_id)
                    if row is None:
                        continue
                    row.last_run_at = entry.model.last_run_at
                    row.total_run_count = entry.model.total_run_count
                    row.enabled = entry.model.enabled
                    due = entry.schedule.remaining_estimate(row.last_run_at or utc_now())
                    row.next_run_at = (row.last_run_at or utc_now()) + due
        except Exception as exc:
            # Losing run bookkeeping is cosmetic; failing here would stop Beat.
            logger.warning("beat.sync_failed", error=str(exc), entries=len(dirty))

    def close(self) -> None:
        self.sync()
        super().close()

    @property
    def info(self) -> str:
        return f"    . database-backed ({len(self._schedule)} entries)"
