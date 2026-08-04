"""The Celery application.

The configuration here is what turns "we use Celery" into "the pipeline never
blocks and never loses work". The settings that matter:

``task_acks_late``
    A message is acknowledged *after* the task finishes, so a worker killed
    mid-task returns the work to the queue instead of silently dropping it.
    This is only safe because every task is idempotent — the master UUID makes
    a replay overwrite rather than duplicate.

``worker_prefetch_multiplier = 1``
    With long, variable-duration tasks, prefetching hands one worker a batch it
    will chew through slowly while its peers idle. One at a time keeps the
    queue fairly distributed.

``task_time_limit`` / ``task_soft_time_limit``
    The soft limit raises inside the task so it can record its own failure; the
    hard limit kills it. Without both, a wedged connector holds a worker slot
    forever, which is precisely the "pipeline blocks" failure we must not have.

``task_reject_on_worker_lost``
    A worker that dies (OOM killer, container eviction) requeues its task.

``broker_transport_options``
    ``visibility_timeout`` must exceed the longest task, otherwise Redis
    redelivers a still-running scraping job and it executes twice.
"""

from __future__ import annotations

from typing import Any

from celery import Celery, signals
from celery.schedules import crontab

from app.core.config import get_settings
from app.core.logging import bind_context, clear_context, configure_logging, get_logger
from app.workers.queues import DEFAULT_QUEUE, QUEUES, TASK_ROUTES

logger = get_logger(__name__)

__all__ = ["MAINTENANCE_SCHEDULE", "celery_app", "create_celery_app"]

#: Always-on reconciliation loops. Cadences are chosen so that each task's cost
#: is negligible relative to its interval, and so that the slowest detection
#: latency an operator can experience is a few minutes rather than a day.
MAINTENANCE_SCHEDULE: dict[str, dict[str, Any]] = {
    "collect-queue-metrics": {
        "task": "app.workers.tasks.maintenance.collect_queue_metrics",
        "schedule": 30.0,
        "options": {"queue": "maintenance", "expires": 25},
    },
    "reconcile-stuck-jobs": {
        "task": "app.workers.tasks.maintenance.reconcile_stuck_jobs",
        "schedule": 600.0,
        "options": {"queue": "maintenance", "expires": 550},
    },
    "requeue-stalled-tenders": {
        "task": "app.workers.tasks.maintenance.requeue_stalled_tenders",
        "schedule": 900.0,
        "options": {"queue": "maintenance", "expires": 850},
    },
    "flush-pending-notifications": {
        "task": "app.workers.tasks.notifications.flush_pending_notifications",
        "schedule": 600.0,
        "options": {"queue": "notifications", "expires": 550},
    },
    "check-connector-health": {
        "task": "app.workers.tasks.maintenance.check_connector_health",
        "schedule": 3600.0,
        "options": {"queue": "maintenance", "expires": 3500},
    },
    "sync-source-registry": {
        "task": "app.workers.tasks.maintenance.sync_source_registry",
        "schedule": 1800.0,
        "options": {"queue": "maintenance", "expires": 1700},
    },
    "purge-old-logs": {
        "task": "app.workers.tasks.maintenance.purge_old_logs",
        "schedule": 86400.0,
        "options": {"queue": "maintenance", "expires": 3600},
    },
    # Weekly: retention is measured in months, so a daily sweep would re-scan
    # the same rows for nothing.
    "purge-expired-tenders": {
        "task": "app.workers.tasks.maintenance.purge_expired_tenders",
        "schedule": 604800.0,
        "options": {"queue": "maintenance", "expires": 3600},
    },
    # Hourly rather than daily: a deadline is a time of day, and a bid manager
    # who opens the dashboard at 14:00 should not still see this morning's
    # 10:00 deadline listed as open.
    "close-expired-tenders": {
        "task": "app.workers.tasks.maintenance.close_expired_tenders",
        "schedule": 3600.0,
        "options": {"queue": "maintenance", "expires": 3500},
    },
    # Digest emails. Crontab rather than an interval so they land at a
    # predictable hour local time instead of drifting with worker restarts.
    # `expires` prevents a digest from being delivered hours late after an
    # outage — a stale "daily" summary is worse than none.
    "daily-digests": {
        "task": "app.workers.tasks.notifications.send_digests",
        "schedule": crontab(hour=7, minute=0),
        "kwargs": {"frequency": "daily"},
        "options": {"queue": "notifications", "expires": 7200},
    },
    "weekly-digests": {
        "task": "app.workers.tasks.notifications.send_digests",
        "schedule": crontab(hour=7, minute=30, day_of_week=1),
        "kwargs": {"frequency": "weekly"},
        "options": {"queue": "notifications", "expires": 21600},
    },
}


def create_celery_app() -> Celery:
    settings = get_settings()
    worker = settings.worker

    app = Celery(
        "smarttender",
        broker=settings.redis.broker_url,
        backend=settings.redis.result_url,
        include=[
            "app.workers.tasks.scraping",
            "app.workers.tasks.pipeline",
            "app.workers.tasks.notifications",
            "app.workers.tasks.maintenance",
        ],
    )

    app.conf.update(
        # --- serialisation -------------------------------------------
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # --- reliability ---------------------------------------------
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_acks_on_failure_or_timeout=True,
        worker_prefetch_multiplier=worker.prefetch_multiplier,
        worker_max_tasks_per_child=worker.max_tasks_per_child,
        task_track_started=True,
        # --- limits ---------------------------------------------------
        task_soft_time_limit=worker.task_soft_time_limit_seconds,
        task_time_limit=worker.task_time_limit_seconds,
        # --- results --------------------------------------------------
        result_expires=worker.result_ttl_seconds,
        result_extended=True,
        # --- routing --------------------------------------------------
        task_default_queue=DEFAULT_QUEUE,
        task_default_exchange="smarttender",
        task_default_routing_key=DEFAULT_QUEUE,
        task_queues=QUEUES,
        task_routes=TASK_ROUTES,
        task_create_missing_queues=True,
        # --- broker ---------------------------------------------------
        broker_transport_options={
            # Longer than the longest task, or Redis redelivers a running job.
            "visibility_timeout": worker.scraping_time_limit_seconds + 600,
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 0.5,
            "interval_max": 5,
        },
        broker_connection_retry_on_startup=True,
        broker_pool_limit=10,
        redis_socket_keepalive=True,
        # --- beat -----------------------------------------------------
        beat_max_loop_interval=worker.beat_max_loop_interval_seconds,
        beat_scheduler="app.workers.beat:DatabaseScheduler",
        # Housekeeping is infrastructure, not policy, so it is pinned in code
        # rather than made editable in the database — an operator must not be
        # able to switch off the reconciliation that keeps the pipeline honest.
        beat_schedule=MAINTENANCE_SCHEDULE,
        # --- misc -----------------------------------------------------
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
        worker_send_task_events=True,
        task_send_sent_event=True,
    )
    return app


celery_app = create_celery_app()


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
@signals.setup_logging.connect
def _setup_logging(**_kwargs: Any) -> None:
    """Stop Celery replacing our structured logging with its own format."""
    configure_logging(force=True)


@signals.worker_ready.connect
def _on_worker_ready(sender: Any = None, **_kwargs: Any) -> None:
    """Reconcile the source registry once the worker is up.

    Failing here must not prevent the worker from serving tasks: a database
    that is briefly unavailable at boot should delay synchronisation, not take
    the worker out of rotation.
    """
    configure_logging()
    try:
        from app.db.session import session_scope
        from app.services.sources import sync_sources

        with session_scope() as session:
            result = sync_sources(session)
        logger.info("worker.ready", hostname=getattr(sender, "hostname", None), **result)
    except Exception as exc:
        logger.warning("worker.source_sync_failed", error=str(exc))


@signals.task_prerun.connect
def _task_prerun(task_id: str | None = None, task: Any = None, **_kwargs: Any) -> None:
    bind_context(task_id=task_id)


@signals.task_postrun.connect
def _task_postrun(**_kwargs: Any) -> None:
    clear_context()


@signals.task_failure.connect
def _task_failure(
    task_id: str | None = None,
    exception: BaseException | None = None,
    sender: Any = None,
    **_kwargs: Any,
) -> None:
    logger.error(
        "task.failed",
        task_id=task_id,
        task_name=getattr(sender, "name", None),
        error_type=type(exception).__name__ if exception else None,
        error=str(exception) if exception else None,
    )


@signals.worker_shutting_down.connect
def _worker_shutdown(**_kwargs: Any) -> None:
    from app.db.session import dispose_sync_engine

    dispose_sync_engine()
    logger.info("worker.shutting_down")
