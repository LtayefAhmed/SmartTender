"""Entry point C — customisable scheduled scraping.

Schedules are rows, not code. Creating, editing, pausing or deleting one takes
effect within one Beat sync interval with nothing restarted, because every
mutation here touches the change sentinel that Beat polls.

``POST /schedules/{id}/run`` fires a schedule immediately without disturbing its
cadence — the "test my configuration" button, and the manual override an
operator reaches for when a deadline is close.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, pagination, require_principal
from app.connectors.registry import get_registry
from app.core.enums import JobTrigger, ScheduleKind
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.db.models.schedule import Schedule, ScheduleChangeSentinel
from app.schemas.common import AcceptedResponse, Page, PaginationParams
from app.schemas.schedule import (
    INTERVAL_PRESETS,
    ScheduleCreate,
    ScheduleRead,
    ScheduleUpdate,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/schedules", tags=["schedules"])


async def _touch_sentinel(session: AsyncSession) -> None:
    """Tell Beat the schedule table changed."""
    sentinel = await session.get(ScheduleChangeSentinel, 1)
    if sentinel is None:
        session.add(ScheduleChangeSentinel(id=1, last_update=utc_now()))
    else:
        sentinel.last_update = utc_now()


def _validate_connectors(connectors: list[str]) -> None:
    if not connectors:
        return
    known = set(get_registry().keys())
    unknown = [key for key in connectors if key not in known]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Unknown connector key(s).",
                "unknown": unknown,
                "known": sorted(known),
            },
        )


@router.get("/presets", summary="Available interval presets")
async def list_presets() -> dict[str, Any]:
    return {
        "presets": [
            {"key": key, "seconds": seconds, "label": key.replace("_", " ")}
            for key, seconds in INTERVAL_PRESETS.items()
        ],
        "note": "Any interval_seconds value or crontab expression is also accepted.",
    }


@router.get("", response_model=Page[ScheduleRead], summary="List schedules")
async def list_schedules(
    session: AsyncSession = Depends(get_session),
    params: PaginationParams = Depends(pagination),
    _: Principal = Depends(require_principal),
) -> Page[ScheduleRead]:
    total = (await session.execute(select(func.count(Schedule.id)))).scalar_one()
    rows = (
        (
            await session.execute(
                select(Schedule)
                .order_by(desc(Schedule.enabled), Schedule.name)
                .offset(params.offset)
                .limit(params.page_size)
            )
        )
        .scalars()
        .all()
    )
    return Page.build([ScheduleRead.model_validate(row) for row in rows], total, params)


@router.post(
    "",
    response_model=ScheduleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a schedule",
)
async def create_schedule(
    payload: ScheduleCreate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> ScheduleRead:
    _validate_connectors(payload.connectors)

    row = Schedule(
        name=payload.name,
        description=payload.description,
        enabled=payload.enabled,
        kind=payload.kind.value,
        interval_seconds=payload.interval_seconds,
        cron_minute=payload.cron_minute,
        cron_hour=payload.cron_hour,
        cron_day_of_week=payload.cron_day_of_week,
        cron_day_of_month=payload.cron_day_of_month,
        cron_month_of_year=payload.cron_month_of_year,
        timezone=payload.timezone,
        connectors=payload.connectors,
        filters=payload.filters.as_payload(),
        queue=payload.queue or "scraping",
        expire_seconds=payload.expire_seconds,
        skip_if_running=payload.skip_if_running,
        one_off=payload.one_off,
        start_after=payload.start_after,
        expires_at=payload.expires_at,
        created_by=principal.identity,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"A schedule named '{payload.name}' already exists.",
        ) from exc

    await _touch_sentinel(session)
    logger.info(
        "api.schedule_created",
        name=row.name,
        kind=row.kind,
        actor=principal.identity,
    )
    return ScheduleRead.model_validate(row)


@router.get("/{schedule_id}", response_model=ScheduleRead, summary="Get a schedule")
async def get_schedule(
    schedule_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_principal),
) -> ScheduleRead:
    row = await session.get(Schedule, schedule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found.")
    return ScheduleRead.model_validate(row)


@router.put("/{schedule_id}", response_model=ScheduleRead, summary="Update a schedule")
async def update_schedule(
    schedule_id: uuid_module.UUID,
    payload: ScheduleUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> ScheduleRead:
    row = await session.get(Schedule, schedule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found.")

    updates = payload.model_dump(exclude_unset=True, exclude={"preset", "filters"})
    if payload.connectors is not None:
        _validate_connectors(payload.connectors)
    if payload.filters is not None:
        row.filters = payload.filters.as_payload()

    for field, value in updates.items():
        setattr(row, field, value)

    if payload.interval_seconds is not None:
        row.kind = ScheduleKind.INTERVAL.value
    elif payload.cron_minute is not None:
        row.kind = ScheduleKind.CRONTAB.value

    await _touch_sentinel(session)
    logger.info("api.schedule_updated", name=row.name, actor=principal.identity)
    return ScheduleRead.model_validate(row)


@router.delete(
    "/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a schedule",
)
async def delete_schedule(
    schedule_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> None:
    row = await session.get(Schedule, schedule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found.")
    name = row.name
    await session.delete(row)
    await _touch_sentinel(session)
    logger.info("api.schedule_deleted", name=name, actor=principal.identity)


@router.post(
    "/{schedule_id}/run",
    response_model=AcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run a schedule now",
)
async def run_schedule_now(
    schedule_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> AcceptedResponse:
    """Fire a schedule immediately without shifting its cadence."""
    row = await session.get(Schedule, schedule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found.")

    job_id = str(uuid_module.uuid4())
    await session.commit()

    from app.workers.tasks.scraping import run_scraping_job

    task = run_scraping_job.apply_async(
        kwargs={
            "job_id": job_id,
            "connectors": list(row.connectors or []),
            "filters": dict(row.filters or {}),
            "trigger": JobTrigger.MANUAL.value,
            "schedule_id": str(schedule_id),
            "requested_by": principal.identity,
        },
        queue=row.queue or "scraping",
    )

    logger.info(
        "api.schedule_run_now", name=row.name, job_id=job_id, actor=principal.identity
    )
    return AcceptedResponse(
        message=f"Schedule '{row.name}' started immediately.",
        job_id=job_id,
        task_id=task.id,
        poll_url=f"/scrape/jobs/{job_id}",
    )


@router.post(
    "/{schedule_id}/toggle",
    response_model=ScheduleRead,
    summary="Enable or disable a schedule",
)
async def toggle_schedule(
    schedule_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> ScheduleRead:
    row = await session.get(Schedule, schedule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found.")
    row.enabled = not row.enabled
    await _touch_sentinel(session)
    logger.info(
        "api.schedule_toggled",
        name=row.name,
        enabled=row.enabled,
        actor=principal.identity,
    )
    return ScheduleRead.model_validate(row)
