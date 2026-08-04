"""Housekeeping tasks.

These exist because a distributed pipeline that only moves forward eventually
accumulates state that nothing owns: jobs whose worker died mid-run, tenders
whose follow-up task was never published because the broker blinked, an audit
table that grows without bound, and connector health that nobody notices has
gone quiet.

Each task is a reconciliation loop: it looks at reality, compares it to what
should be true, and repairs the difference. That is what makes the pipeline
self-healing rather than merely resilient.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from app.core.enums import JobStatus, SourceHealth, TenderPipelineState, TenderStatus
from app.core.identity import as_utc, utc_now
from app.core.logging import get_logger
from app.core.metrics import connector_health, queue_size
from app.db.models.job import ConnectorRun, ScrapingJob
from app.db.models.log import ExecutionLog
from app.db.models.source import Source
from app.db.models.tender import Tender
from app.db.session import session_scope
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = [
    "check_connector_health",
    "close_expired_tenders",
    "collect_queue_metrics",
    "purge_expired_tenders",
    "purge_old_logs",
    "reconcile_stuck_jobs",
    "requeue_stalled_tenders",
    "sync_source_registry",
]


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.maintenance.close_expired_tenders",
    queue="maintenance",
)
def close_expired_tenders(self: PipelineTask, grace_hours: int = 0) -> dict[str, Any]:
    """Move tenders past their submission deadline to CLOSED.

    Without this, a tender stays OPEN forever and the dashboard's headline
    figure slowly becomes a count of everything ever seen rather than of what
    can still be bid on — which is the only number a bid manager actually acts
    on. Portals rarely publish a withdrawal notice, so the deadline we already
    hold is the signal.

    Nothing is deleted. A closed tender is still evidence: it feeds duplicate
    detection when the same notice is re-published, and it is the raw material
    for the win/loss history that later scoring and the CV-matching module
    depend on. Expiry is a status change, never a purge.
    """
    cutoff = utc_now() - timedelta(hours=grace_hours)
    with session_scope() as session:
        result = session.execute(
            update(Tender)
            .where(
                Tender.deadline.is_not(None),
                Tender.deadline < cutoff,
                Tender.status.in_([TenderStatus.OPEN.value, TenderStatus.UNKNOWN.value]),
            )
            .values(status=TenderStatus.CLOSED.value)
        )
        closed = result.rowcount or 0

    if closed:
        logger.info("maintenance.tenders_closed", count=closed)
    return {"closed": closed}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.maintenance.purge_expired_tenders",
    queue="maintenance",
)
def purge_expired_tenders(
    self: PipelineTask, retention_months: int = 12, batch: int = 2_000
) -> dict[str, Any]:
    """Delete tenders whose deadline passed more than `retention_months` ago.

    The only task permitted to delete tenders, and the counterpart to
    `close_expired_tenders`: expiry changes a status, this reclaims the row
    once the record has stopped being useful.

    Twelve months is the configured window. The trade-off is worth naming: an
    archived tender is what duplicate detection compares against when the same
    notice is re-published, and it is the raw material for the win/loss history
    that later scoring depends on. Past a year both uses fade — a re-issued
    contract is a new opportunity, not a duplicate — but shortening this window
    further would start costing signal.

    Documents in object storage are removed with the rows; nothing is left
    orphaned in MinIO paying for itself indefinitely.
    """
    from app.db.models.tender import TenderDocument

    cutoff = utc_now() - timedelta(days=int(retention_months * 30.44))
    storage_keys: list[str] = []

    with session_scope() as session:
        ids = list(
            session.execute(
                select(Tender.id)
                .where(Tender.deadline.is_not(None), Tender.deadline < cutoff)
                .limit(batch)
            )
            .scalars()
            .all()
        )
        if not ids:
            return {"purged": 0}

        storage_keys = [
            key
            for key in session.execute(
                select(TenderDocument.storage_key).where(TenderDocument.tender_id.in_(ids))
            )
            .scalars()
            .all()
            if key
        ]
        session.execute(delete(Tender).where(Tender.id.in_(ids)))

    # Storage is cleaned after the rows are gone: a delete that fails here
    # leaves an orphaned object, which is wasteful; the reverse would leave a
    # row pointing at nothing, which looks like corruption.
    if storage_keys:
        try:
            from app.services.storage import get_storage

            storage = get_storage()
            for key in storage_keys:
                storage.delete(key)
        except Exception as exc:
            logger.warning("maintenance.purge_storage_failed", error=str(exc)[:200])

    logger.info(
        "maintenance.tenders_purged",
        count=len(ids),
        documents=len(storage_keys),
        retention_months=retention_months,
    )
    return {"purged": len(ids), "documents": len(storage_keys)}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.maintenance.reconcile_stuck_jobs",
    queue="maintenance",
)
def reconcile_stuck_jobs(self: PipelineTask, timeout_minutes: int = 90) -> dict[str, Any]:
    """Close out jobs whose workers died without reporting.

    Without this, a killed worker leaves a job RUNNING forever, which then
    blocks its schedule's ``skip_if_running`` guard and silently stops that
    source from ever being scraped again.
    """
    cutoff = utc_now() - timedelta(minutes=timeout_minutes)
    closed_runs = closed_jobs = 0

    with session_scope() as session:
        stale_runs = list(
            session.execute(
                select(ConnectorRun).where(
                    ConnectorRun.status.in_(
                        [JobStatus.PENDING.value, JobStatus.RUNNING.value]
                    ),
                    ConnectorRun.created_at <= cutoff,
                )
            )
            .scalars()
            .all()
        )
        for run in stale_runs:
            run.status = JobStatus.TIMED_OUT.value
            run.finished_at = utc_now()
            run.error_type = "TimedOut"
            run.error_message = (
                f"No completion reported within {timeout_minutes} minutes; "
                "the worker most likely died."
            )
            closed_runs += 1

        # Make the run updates visible to the job query below. Sessions here
        # run with autoflush disabled, so without this the count of unfinished
        # runs still sees the old statuses — and the jobs this task exists to
        # close would stay RUNNING forever, permanently blocking their
        # schedule's `skip_if_running` guard.
        session.flush()

        stale_jobs = list(
            session.execute(
                select(ScrapingJob).where(
                    ScrapingJob.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
                    ScrapingJob.created_at <= cutoff,
                )
            )
            .scalars()
            .all()
        )
        for job in stale_jobs:
            unfinished = session.execute(
                select(func.count(ConnectorRun.id)).where(
                    ConnectorRun.job_id == job.id,
                    ConnectorRun.status.in_(
                        [JobStatus.PENDING.value, JobStatus.RUNNING.value]
                    ),
                )
            ).scalar_one()
            if unfinished:
                continue
            job.status = (
                JobStatus.TIMED_OUT.value
                if job.connectors_succeeded == 0
                else job.derive_status()
            )
            job.finished_at = utc_now()
            closed_jobs += 1

    if closed_runs or closed_jobs:
        logger.warning(
            "maintenance.stuck_jobs_reconciled", runs=closed_runs, jobs=closed_jobs
        )
    return {"runs_closed": closed_runs, "jobs_closed": closed_jobs}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.maintenance.requeue_stalled_tenders",
    queue="maintenance",
)
def requeue_stalled_tenders(self: PipelineTask, older_than_minutes: int = 30, limit: int = 500):
    """Re-publish pipeline tasks for tenders that never progressed.

    Covers the one gap in enqueue-after-commit: the row committed but the
    broker publish failed. The tender is safe in the database; this puts it
    back on the queue.
    """
    from app.workers.tasks.pipeline import process_tender

    cutoff = utc_now() - timedelta(minutes=older_than_minutes)
    stalled_states = (
        TenderPipelineState.RECEIVED.value,
        TenderPipelineState.QUEUED.value,
        TenderPipelineState.PARSING.value,
        TenderPipelineState.SCORING.value,
    )

    with session_scope() as session:
        stalled = list(
            session.execute(
                select(Tender.id)
                .where(Tender.pipeline_state.in_(stalled_states), Tender.created_at <= cutoff)
                .limit(limit)
            )
            .scalars()
            .all()
        )

    for tender_id in stalled:
        process_tender.apply_async(kwargs={"tender_id": str(tender_id)}, queue="parsing")

    if stalled:
        logger.warning("maintenance.stalled_tenders_requeued", count=len(stalled))
    return {"requeued": len(stalled)}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.maintenance.collect_queue_metrics",
    queue="maintenance",
)
def collect_queue_metrics(self: PipelineTask) -> dict[str, int]:
    """Export queue depth to Prometheus.

    Queue depth is the platform's single best saturation signal: rising depth
    means arrival rate has overtaken processing rate, which is the alert you
    want *before* latency becomes visible to users.
    """
    import redis

    from app.core.config import get_settings
    from app.workers.queues import QUEUE_NAMES

    settings = get_settings()
    depths: dict[str, int] = {}
    try:
        client = redis.Redis.from_url(
            settings.redis.broker_url, socket_timeout=settings.redis.socket_timeout_seconds
        )
        for name in QUEUE_NAMES:
            depth = int(client.llen(name) or 0)
            depths[name] = depth
            queue_size.labels(queue=name).set(depth)
        client.close()
    except Exception as exc:
        logger.warning("maintenance.queue_metrics_failed", error=str(exc))
    return depths


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.maintenance.check_connector_health",
    queue="maintenance",
)
def check_connector_health(self: PipelineTask, silence_hours: int = 26) -> dict[str, Any]:
    """Alert on sources that have gone quiet or are failing.

    Silence is the dangerous failure mode. A source that errors is loud; a
    source that has simply not run — because its schedule was disabled, its
    circuit stayed open, or its worker queue is not being consumed — produces
    no signal at all until someone notices the dashboard is stale.
    """
    cutoff = utc_now() - timedelta(hours=silence_hours)
    alerts: list[dict[str, Any]] = []

    with session_scope() as session:
        sources = list(
            session.execute(select(Source).where(Source.enabled.is_(True))).scalars().all()
        )
        for source in sources:
            if source.health in (
                SourceHealth.DISABLED.value,
                SourceHealth.CREDENTIALS_MISSING.value,
            ):
                continue

            # as_utc: a driver that returns a naive timestamp would otherwise
            # raise here and take the whole health sweep down with it.
            last_run = as_utc(source.last_run_at)
            if last_run is None or last_run <= cutoff:
                alerts.append(
                    {
                        "connector": source.key,
                        "type": "silent",
                        "detail": (
                            f"No run since "
                            f"{last_run.isoformat() if last_run else 'ever'}."
                        ),
                    }
                )
            if source.health == SourceHealth.FAILING.value:
                alerts.append(
                    {
                        "connector": source.key,
                        "type": "failing",
                        "detail": source.health_reason or "consecutive failures",
                    }
                )
            elif source.health == SourceHealth.DEGRADED.value:
                alerts.append(
                    {
                        "connector": source.key,
                        "type": "degraded",
                        "detail": source.health_reason or "",
                    }
                )
            connector_health.labels(connector=source.key).set(
                1.0 if source.health == SourceHealth.HEALTHY.value else 0.5
            )

    for alert in alerts:
        logger.warning("connector.health_alert", **alert)
    return {"alerts": alerts, "count": len(alerts)}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.maintenance.purge_old_logs",
    queue="maintenance",
)
def purge_old_logs(self: PipelineTask, retention_days: int = 180, batch: int = 10_000):
    """Trim the audit table. The only task permitted to delete audit rows."""
    cutoff = utc_now() - timedelta(days=retention_days)
    with session_scope() as session:
        ids = list(
            session.execute(
                select(ExecutionLog.id)
                .where(ExecutionLog.ts < cutoff, ExecutionLog.level.in_(["DEBUG", "INFO"]))
                .limit(batch)
            )
            .scalars()
            .all()
        )
        if ids:
            session.execute(delete(ExecutionLog).where(ExecutionLog.id.in_(ids)))

    if ids:
        logger.info("maintenance.logs_purged", count=len(ids), retention_days=retention_days)
    return {"purged": len(ids)}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.maintenance.sync_source_registry",
    queue="maintenance",
)
def sync_source_registry(self: PipelineTask) -> dict[str, int]:
    """Re-read connector configuration and reconcile the ``sources`` table."""
    from app.core.config import reload_yaml_configs
    from app.services.sources import sync_sources

    reload_yaml_configs()
    with session_scope() as session:
        return sync_sources(session)
