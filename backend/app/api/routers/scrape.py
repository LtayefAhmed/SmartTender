"""Entry point A — manual, filtered scraping launched from the UI.

The endpoint does almost nothing: it validates, records a job row, publishes
one Celery message and returns ``202``. Total server time is a few
milliseconds regardless of how many portals were requested or how slow they
are, which is the API-level expression of "the pipeline never blocks".

Progress is reported asynchronously through ``GET /scrape/jobs/{id}``, whose
``progress`` and per-connector breakdown let the UI show live status — and, more
usefully, show *which* source failed while the others kept going.
"""

from __future__ import annotations

import uuid as uuid_module

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Principal, get_session, pagination, require_principal
from app.connectors.registry import get_registry
from app.core.enums import JobStatus, JobTrigger
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.db.models.job import ScrapingJob
from app.schemas.common import AcceptedResponse, Page, PaginationParams
from app.schemas.scrape import ScrapeJobRead, ScrapeRequest

logger = get_logger(__name__)
router = APIRouter(prefix="/scrape", tags=["scraping"])


@router.post(
    "",
    response_model=AcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Launch a scraping job",
)
async def launch_scrape(
    payload: ScrapeRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> AcceptedResponse:
    """Start a filtered scrape across one or more sources."""
    registry = get_registry()
    runnable, skipped = registry.resolve_requested(payload.connectors or None)

    if not runnable:
        # Nothing to run is a client error worth being explicit about: usually
        # it means every requested source is disabled or lacks credentials.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "None of the requested connectors are currently available.",
                "requested": payload.connectors,
                "skipped": skipped,
                "available": registry.available_keys(),
            },
        )

    filters = payload.filters.resolved()
    job = ScrapingJob(
        id=uuid_module.uuid4(),
        trigger=JobTrigger.MANUAL.value,
        status=JobStatus.PENDING.value,
        requested_connectors=payload.connectors or [],
        filters=filters.as_payload(),
        requested_by=principal.identity,
        connectors_total=len(runnable),
    )
    session.add(job)
    await session.flush()
    job_id = str(job.id)

    # Commit before publishing: a worker must never be able to look up a job
    # row that is not yet visible.
    await session.commit()

    from app.workers.tasks.scraping import run_scraping_job

    task = run_scraping_job.apply_async(
        kwargs={
            "job_id": job_id,
            "connectors": payload.connectors or [],
            "filters": filters.as_payload(),
            "trigger": JobTrigger.MANUAL.value,
            "requested_by": principal.identity,
        },
        queue="scraping",
    )

    logger.info(
        "api.scrape_launched",
        job_id=job_id,
        connectors=runnable,
        skipped=skipped,
        actor=principal.identity,
    )
    return AcceptedResponse(
        message=(
            f"Scraping started across {len(runnable)} source(s)."
            + (f" {len(skipped)} unavailable source(s) were skipped." if skipped else "")
        ),
        job_id=job_id,
        task_id=task.id,
        poll_url=f"/scrape/jobs/{job_id}",
    )


@router.get(
    "/jobs",
    response_model=Page[ScrapeJobRead],
    summary="List scraping jobs",
)
async def list_jobs(
    session: AsyncSession = Depends(get_session),
    params: PaginationParams = Depends(pagination),
    job_status: str | None = Query(default=None, alias="status"),
    trigger: str | None = Query(default=None),
    _: Principal = Depends(require_principal),
) -> Page[ScrapeJobRead]:
    query = select(ScrapingJob).options(selectinload(ScrapingJob.runs))
    count_query = select(func.count(ScrapingJob.id))

    if job_status:
        query = query.where(ScrapingJob.status == job_status)
        count_query = count_query.where(ScrapingJob.status == job_status)
    if trigger:
        query = query.where(ScrapingJob.trigger == trigger)
        count_query = count_query.where(ScrapingJob.trigger == trigger)

    total = (await session.execute(count_query)).scalar_one()
    rows = (
        (
            await session.execute(
                query.order_by(desc(ScrapingJob.created_at))
                .offset(params.offset)
                .limit(params.page_size)
            )
        )
        .scalars()
        .all()
    )
    return Page.build([ScrapeJobRead.model_validate(row) for row in rows], total, params)


@router.get(
    "/jobs/{job_id}",
    response_model=ScrapeJobRead,
    summary="Get a scraping job",
)
async def get_job(
    job_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_principal),
) -> ScrapeJobRead:
    row = (
        await session.execute(
            select(ScrapingJob)
            .options(selectinload(ScrapingJob.runs))
            .where(ScrapingJob.id == job_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scraping job not found.")
    return ScrapeJobRead.model_validate(row)


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=ScrapeJobRead,
    summary="Cancel a scraping job",
)
async def cancel_job(
    job_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> ScrapeJobRead:
    """Mark a job cancelled and revoke its outstanding tasks.

    Already-running connectors are allowed to finish their current page rather
    than being killed mid-write — an interrupted run that leaves half a page
    unrecorded is worse than a few seconds of extra work.
    """
    row = (
        await session.execute(
            select(ScrapingJob)
            .options(selectinload(ScrapingJob.runs))
            .where(ScrapingJob.id == job_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scraping job not found.")
    if row.status not in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Job is already in the terminal state '{row.status}'.",
        )

    from app.workers.celery_app import celery_app

    for run in row.runs:
        if run.celery_task_id and run.status == JobStatus.PENDING.value:
            celery_app.control.revoke(run.celery_task_id)
            run.status = JobStatus.CANCELLED.value
            run.finished_at = utc_now()

    row.status = JobStatus.CANCELLED.value
    row.finished_at = utc_now()
    logger.info("api.scrape_cancelled", job_id=str(job_id), actor=principal.identity)
    return ScrapeJobRead.model_validate(row)
