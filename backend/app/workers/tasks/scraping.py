"""Scraping tasks — where the isolation invariant is actually enforced.

A job fans out into one independent task per connector. There is deliberately
**no chord and no result-dependency** between them: a chord's callback is
skipped when any member fails, which would mean one broken portal suppressing
the results of every healthy one — exactly the coupling this architecture
exists to prevent.

Instead each connector task, on completion, atomically folds its counters into
the job row and the last one to finish derives the final status. The database
is the aggregation point, so the fan-out survives worker restarts, retries and
partial failures.

``run_connector`` cannot fail the job. ``BaseConnector.run`` already converts
every error into an outcome; this task's own try/except is the second layer,
covering failures in *persistence* rather than in scraping.
"""

from __future__ import annotations

import asyncio
import uuid as uuid_module
from typing import Any

from celery import group
from sqlalchemy import select

from app.connectors.base import ConnectorContext
from app.connectors.models import ConnectorOutcome
from app.connectors.registry import get_registry
from app.core.config import get_settings
from app.core.enums import EntryPoint, JobStatus, JobTrigger, PipelineStage, coerce
from app.core.identity import utc_now
from app.core.logging import get_logger, log_context
from app.db.models.job import ConnectorRun, ScrapingJob
from app.db.models.schedule import Schedule
from app.db.session import session_scope
from app.schemas.filters import TenderFilters
from app.services import audit
from app.services import sources as source_service
from app.services.ingestion import IngestionService
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = ["run_connector", "run_scraping_job"]

_TERMINAL_RUN_STATES = {
    JobStatus.SUCCEEDED.value,
    JobStatus.FAILED.value,
    JobStatus.PARTIAL.value,
    JobStatus.CANCELLED.value,
    JobStatus.TIMED_OUT.value,
}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.scraping.run_scraping_job",
    queue="scraping",
)
def run_scraping_job(
    self: PipelineTask,
    job_id: str | None = None,
    connectors: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    trigger: str = JobTrigger.MANUAL.value,
    schedule_id: str | None = None,
    requested_by: str | None = None,
) -> dict[str, Any]:
    """Plan a scraping job and fan it out, one task per connector.

    Returns immediately after dispatch — it never waits for the connectors,
    so a job over a slow portal does not occupy a worker slot for an hour.
    """
    registry = get_registry()
    trigger_enum = coerce(JobTrigger, trigger, JobTrigger.MANUAL)

    try:
        parsed_filters = TenderFilters(**(filters or {}))
    except Exception as exc:
        logger.error("scraping.invalid_filters", error=str(exc))
        parsed_filters = TenderFilters()

    runnable, skipped = registry.resolve_requested(connectors)

    with session_scope() as session:
        job = _load_or_create_job(
            session,
            job_id=job_id,
            trigger=trigger_enum,
            connectors=connectors or [],
            filters=parsed_filters,
            schedule_id=schedule_id,
            requested_by=requested_by,
            celery_task_id=self.request.id,
        )
        job.status = JobStatus.RUNNING.value
        job.started_at = utc_now()
        job.connectors_total = len(runnable)
        job.connectors_skipped = len(skipped)

        if skipped:
            job.errors = [
                *(job.errors or []),
                *[
                    {
                        "connector": key,
                        "error_type": "Skipped",
                        "message": "Connector is unavailable (disabled, unknown, or "
                        "missing credentials).",
                    }
                    for key in skipped
                ],
            ]

        run_ids: list[tuple[str, str]] = []
        for key in runnable:
            run = ConnectorRun(
                job_id=job.id,
                connector_key=key,
                status=JobStatus.PENDING.value,
                filters=parsed_filters.as_payload(),
            )
            source = source_service.get_or_create_source(session, key)
            run.source_id = source.id
            session.add(run)
            session.flush()
            run_ids.append((key, str(run.id)))

        if schedule_id:
            schedule = session.get(Schedule, uuid_module.UUID(schedule_id))
            if schedule is not None:
                schedule.last_job_id = job.id

        audit.record_event(
            session,
            "scraping.job_planned",
            stage=PipelineStage.FETCH,
            job_id=job.id,
            actor=requested_by,
            message=f"{len(runnable)} connector(s) dispatched, {len(skipped)} skipped.",
            context={
                "connectors": runnable,
                "skipped": skipped,
                "trigger": trigger_enum.value,
            },
        )

        job_uuid = str(job.id)
        if not runnable:
            job.status = JobStatus.SUCCEEDED.value
            job.finished_at = utc_now()
            logger.warning("scraping.no_runnable_connectors", job_id=job_uuid, skipped=skipped)
            return {"job_id": job_uuid, "dispatched": 0, "skipped": skipped}

    # Dispatch outside the transaction so a worker can never pick up a run
    # before its row is visible.
    group(
        run_connector.s(
            job_id=job_uuid,
            run_id=run_id,
            connector_key=key,
            filters=parsed_filters.as_payload(),
            trigger=trigger_enum.value,
        )
        for key, run_id in run_ids
    ).apply_async()

    logger.info(
        "scraping.job_dispatched",
        job_id=job_uuid,
        connectors=len(run_ids),
        skipped=len(skipped),
        trigger=trigger_enum.value,
    )
    return {"job_id": job_uuid, "dispatched": len(run_ids), "skipped": skipped}


def _load_or_create_job(
    session: Any,
    *,
    job_id: str | None,
    trigger: JobTrigger,
    connectors: list[str],
    filters: TenderFilters,
    schedule_id: str | None,
    requested_by: str | None,
    celery_task_id: str | None,
) -> ScrapingJob:
    if job_id:
        existing = session.get(ScrapingJob, uuid_module.UUID(job_id))
        if existing is not None:
            existing.celery_task_id = celery_task_id
            return existing

    job = ScrapingJob(
        id=uuid_module.UUID(job_id) if job_id else uuid_module.uuid4(),
        trigger=trigger.value,
        status=JobStatus.PENDING.value,
        requested_connectors=connectors,
        filters=filters.as_payload(),
        requested_by=requested_by,
        schedule_id=uuid_module.UUID(schedule_id) if schedule_id else None,
        celery_task_id=celery_task_id,
    )
    session.add(job)
    session.flush()
    return job


# ---------------------------------------------------------------------------
@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.scraping.run_connector",
    queue="scraping",
    soft_time_limit=None,   # set from settings below, at call time
    max_retries=2,
)
def run_connector(
    self: PipelineTask,
    job_id: str,
    run_id: str,
    connector_key: str,
    filters: dict[str, Any] | None = None,
    trigger: str = JobTrigger.MANUAL.value,
) -> dict[str, Any]:
    """Run exactly one connector and persist everything it produced.

    Never raises for a scraping failure: the outcome is recorded and the job
    continues. It *may* raise for an infrastructure failure (database gone), in
    which case Celery retries the connector alone.
    """
    settings = get_settings()
    trigger_enum = coerce(JobTrigger, trigger, JobTrigger.MANUAL)
    parsed_filters = TenderFilters(**(filters or {}))

    with log_context(connector=connector_key, job_id=job_id, task_id=self.request.id):
        context = ConnectorContext(
            job_id=job_id,
            run_id=run_id,
            trigger=trigger_enum,
            # Stop a little before the hard limit so the run can record its own
            # partial results instead of being killed mid-write.
            deadline_seconds=float(settings.worker.scraping_soft_time_limit_seconds) - 60.0,
            max_items=parsed_filters.max_results_per_source,
            max_pages=parsed_filters.max_pages,
            allow_private_hosts=not settings.is_production,
        )

        with session_scope() as session:
            run = session.get(ConnectorRun, uuid_module.UUID(run_id))
            if run is not None:
                run.status = JobStatus.RUNNING.value
                run.started_at = utc_now()
                run.celery_task_id = self.request.id
                run.retry_count = self.request.retries or 0

        # --- scrape ---------------------------------------------------
        try:
            connector = get_registry().create(connector_key, context)
            outcome = asyncio.run(connector.run(parsed_filters))
        except Exception as exc:
            # Construction failed (bad config, missing implementation). Still an
            # outcome, still isolated.
            logger.exception("connector.construction_failed", connector=connector_key)
            outcome = ConnectorOutcome(
                connector_key=connector_key,
                succeeded=False,
                error_type=type(exc).__name__,
                error_message=str(exc)[:1000],
            )

        # --- persist --------------------------------------------------
        try:
            summary = _persist_outcome(
                job_id=job_id,
                run_id=run_id,
                outcome=outcome,
                trigger=trigger_enum,
            )
        except Exception as exc:
            # Infrastructure failure: worth retrying this connector alone.
            logger.exception("connector.persist_failed", connector=connector_key)
            self.retry_or_fail(exc)
            raise  # unreachable; keeps type checkers happy

        _finalize_job_if_complete(job_id)
        return summary


def _persist_outcome(
    *,
    job_id: str,
    run_id: str,
    outcome: ConnectorOutcome,
    trigger: JobTrigger,
) -> dict[str, Any]:
    """Ingest the tenders and fold the counters into the run, source and job."""
    entry_point = (
        EntryPoint.SCHEDULED_SCRAPE
        if trigger is JobTrigger.SCHEDULED
        else EntryPoint.MANUAL_SCRAPE
    )
    ingestion = IngestionService()
    job_uuid = uuid_module.UUID(job_id)

    ingested = duplicates = rejected = 0

    with session_scope() as session:
        run = session.get(ConnectorRun, uuid_module.UUID(run_id))
        source = source_service.get_or_create_source(session, outcome.connector_key)

        for tender in outcome.tenders:
            try:
                result = ingestion.ingest_tender(
                    session,
                    tender,
                    entry_point=entry_point,
                    source=source,
                    job_id=job_uuid,
                    enqueue=_enqueue_processing,
                )
            except Exception as exc:
                # One bad tender must not abort the other 499.
                rejected += 1
                logger.warning(
                    "ingestion.item_failed",
                    connector=outcome.connector_key,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
                audit.record_error(
                    session,
                    "ingestion.item_failed",
                    exc,
                    stage=PipelineStage.INGEST,
                    connector=outcome.connector_key,
                    job_id=job_uuid,
                    url=tender.source_url,
                )
                continue

            if result.accepted:
                ingested += 1
            elif result.reason == "duplicate":
                duplicates += 1
            else:
                rejected += 1

        source.total_duplicates = (source.total_duplicates or 0) + duplicates
        source_service.apply_outcome(session, source, outcome)

        if run is not None:
            run.status = _run_status(outcome)
            run.finished_at = utc_now()
            run.duration_seconds = outcome.duration_seconds
            run.pages_fetched = outcome.pages_fetched
            run.http_requests = outcome.http_requests
            run.http_retries = outcome.http_retries
            run.bytes_downloaded = outcome.bytes_downloaded
            run.items_found = outcome.items_found
            run.items_ingested = ingested
            run.items_duplicate = duplicates
            run.items_rejected = rejected
            run.items_failed = len(outcome.item_failures)
            run.error_type = outcome.error_type
            run.error_message = outcome.error_message
            run.error_context = outcome.error_context
            run.item_errors = [failure.to_dict() for failure in outcome.item_failures]
            # Diagnostic counters live in `extra` rather than in columns: they
            # exist to explain a zero-result run, not to be aggregated over. The
            # distinction they draw is the important one — "the selectors read
            # 200 rows and your filters kept none" versus "we parsed nothing and
            # are silently blind" are the same `items_found: 0` without them.
            run.extra = {
                **(run.extra or {}),
                "records_parsed": outcome.records_parsed,
                "items_filtered_out": outcome.items_filtered_out,
                "items_duplicate_in_run": outcome.items_duplicate_in_run,
                "stop_reason": outcome.stop_reason,
                "skip_reason": outcome.skip_reason,
            }
            if outcome.filter_application:
                run.extra["filter_application"] = outcome.filter_application.model_dump()

        job = session.get(ScrapingJob, job_uuid)
        if job is not None:
            # Counters are incremented, never assigned, because several
            # connector tasks reach this point concurrently.
            if outcome.skipped:
                job.connectors_skipped = (job.connectors_skipped or 0) + 1
            elif outcome.succeeded:
                job.connectors_succeeded = (job.connectors_succeeded or 0) + 1
            else:
                job.connectors_failed = (job.connectors_failed or 0) + 1
                job.errors = [
                    *(job.errors or []),
                    {
                        "connector": outcome.connector_key,
                        "error_type": outcome.error_type,
                        "message": (outcome.error_message or "")[:500],
                    },
                ]
            job.items_found = (job.items_found or 0) + outcome.items_found
            job.items_ingested = (job.items_ingested or 0) + ingested
            job.items_duplicate = (job.items_duplicate or 0) + duplicates
            job.items_rejected = (job.items_rejected or 0) + rejected

        audit.record_event(
            session,
            "scraping.connector_finished",
            level="INFO" if outcome.succeeded else "ERROR",
            stage=PipelineStage.FETCH,
            connector=outcome.connector_key,
            job_id=job_uuid,
            run_id=uuid_module.UUID(run_id),
            duration_ms=outcome.duration_seconds * 1000,
            error_type=outcome.error_type,
            message=outcome.error_message,
            context={
                **outcome.to_summary(),
                "ingested": ingested,
                "duplicates": duplicates,
                "rejected": rejected,
            },
        )

    return {
        "connector": outcome.connector_key,
        "succeeded": outcome.succeeded,
        "skipped": outcome.skipped,
        "items_found": outcome.items_found,
        "ingested": ingested,
        "duplicates": duplicates,
        "rejected": rejected,
    }


def _run_status(outcome: ConnectorOutcome) -> str:
    if outcome.skipped:
        return JobStatus.CANCELLED.value
    if not outcome.succeeded:
        return JobStatus.FAILED.value
    if outcome.item_failures:
        return JobStatus.PARTIAL.value
    return JobStatus.SUCCEEDED.value


def _finalize_job_if_complete(job_id: str) -> None:
    """Derive the job's terminal status once every connector has finished."""
    with session_scope() as session:
        job = session.get(ScrapingJob, uuid_module.UUID(job_id))
        if job is None or job.status in _TERMINAL_RUN_STATES:
            return

        pending = session.execute(
            select(ConnectorRun.id).where(
                ConnectorRun.job_id == job.id,
                ConnectorRun.status.notin_(tuple(_TERMINAL_RUN_STATES)),
            )
        ).first()
        if pending is not None:
            return

        job.status = job.derive_status()
        job.finished_at = utc_now()
        if job.started_at:
            job.duration_seconds = (job.finished_at - job.started_at).total_seconds()

        audit.record_event(
            session,
            "scraping.job_finished",
            level="INFO" if job.status != JobStatus.FAILED.value else "ERROR",
            stage=PipelineStage.FETCH,
            job_id=job.id,
            duration_ms=(job.duration_seconds or 0) * 1000,
            message=f"Job finished with status '{job.status}'.",
            context={
                "succeeded": job.connectors_succeeded,
                "failed": job.connectors_failed,
                "skipped": job.connectors_skipped,
                "items_ingested": job.items_ingested,
                "items_duplicate": job.items_duplicate,
            },
        )
        logger.info(
            "scraping.job_finished",
            job_id=str(job.id),
            status=job.status,
            ingested=job.items_ingested,
            duplicates=job.items_duplicate,
        )


def _enqueue_processing(tender_id: uuid_module.UUID) -> None:
    """Publish the downstream pipeline task for a freshly accepted tender."""
    from app.workers.tasks.pipeline import process_tender

    process_tender.apply_async(kwargs={"tender_id": str(tender_id)}, queue="parsing")
