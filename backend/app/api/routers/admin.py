"""Admin endpoints: scoring weights, re-scoring and pipeline diagnostics."""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, pagination, require_principal
from app.core.logging import get_logger
from app.db.models.log import ExecutionLog
from app.db.models.tender import DuplicateRecord
from app.schemas.common import AcceptedResponse, Page, PaginationParams

logger = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/scoring/profile", summary="Current scoring profile")
async def scoring_profile(_: Principal = Depends(require_principal)) -> dict[str, Any]:
    """Weights, bands and criteria currently in force.

    Exposed so the admin console can render the weighting UI from the same
    source of truth the engine uses, rather than duplicating it in the frontend.
    """
    from app.services.scoring import get_scoring_engine

    engine = get_scoring_engine()
    return {
        "name": engine.name,
        "version": engine.version,
        "weights": engine.weights,
        "bands": engine.band_metadata(),
        "criteria": list(engine.criteria_config),
    }


@router.post("/scoring/reload", summary="Reload the scoring profile from disk")
async def reload_scoring(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    """Pick up an edited ``scoring.yaml`` without a restart.

    Only affects tenders scored *after* the reload; use ``/admin/scoring/rescore``
    to re-evaluate existing ones.
    """
    from app.core.config import reload_yaml_configs
    from app.services.scoring import get_scoring_engine, reset_scoring_engine

    reload_yaml_configs()
    reset_scoring_engine()
    engine = get_scoring_engine()
    logger.info(
        "api.scoring_reloaded", version=engine.version, actor=principal.identity
    )
    return {"reloaded": True, "version": engine.version, "weights": engine.weights}


@router.post(
    "/scoring/rescore",
    response_model=AcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-score existing tenders",
)
async def rescore(
    limit: int = Query(default=1000, ge=1, le=50_000),
    band: str | None = Query(default=None),
    principal: Principal = Depends(require_principal),
) -> AcceptedResponse:
    """Queue a re-scoring sweep after a weights change.

    Dispatched as individual per-tender tasks so the sweep is parallel,
    interruptible, and cannot monopolise a worker.
    """
    from app.workers.tasks.pipeline import rescore_all

    task = rescore_all.apply_async(
        kwargs={"limit": limit, "band": band}, queue="scoring"
    )
    logger.info("api.rescore_requested", limit=limit, actor=principal.identity)
    return AcceptedResponse(
        message=f"Re-scoring queued for up to {limit} tender(s).",
        task_id=task.id,
    )


@router.get("/duplicates", summary="Recently rejected duplicates")
async def list_duplicates(
    session: AsyncSession = Depends(get_session),
    params: PaginationParams = Depends(pagination),
    strategy: str | None = Query(default=None),
    _: Principal = Depends(require_principal),
) -> Page[dict[str, Any]]:
    """Answers "why is this tender not in the list?".

    Duplicates are invisible in the dashboard by design, so without this view
    a correctly-rejected duplicate is indistinguishable from a lost tender.
    """
    conditions = []
    if strategy:
        conditions.append(DuplicateRecord.strategy == strategy)

    count_query = select(func.count(DuplicateRecord.id))
    query = select(DuplicateRecord)
    if conditions:
        count_query = count_query.where(*conditions)
        query = query.where(*conditions)

    total = (await session.execute(count_query)).scalar_one()
    rows = (
        (
            await session.execute(
                query.order_by(desc(DuplicateRecord.created_at))
                .offset(params.offset)
                .limit(params.page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [
        {
            "id": str(row.id),
            "canonical_tender_id": str(row.canonical_tender_id)
            if row.canonical_tender_id
            else None,
            "strategy": row.strategy,
            "similarity": row.similarity,
            "source_key": row.source_key,
            "title": row.title,
            "source_url": row.source_url,
            "detected_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
    return Page.build(items, total, params)


@router.get("/logs", summary="Query the audit trail")
async def query_logs(
    session: AsyncSession = Depends(get_session),
    params: PaginationParams = Depends(pagination),
    tender_id: uuid_module.UUID | None = Query(default=None),
    job_id: uuid_module.UUID | None = Query(default=None),
    connector: str | None = Query(default=None),
    event: str | None = Query(default=None),
    level: str | None = Query(default=None),
    _: Principal = Depends(require_principal),
) -> Page[dict[str, Any]]:
    """Reconstruct the journey of any tender, job or connector."""
    conditions = []
    if tender_id:
        conditions.append(ExecutionLog.tender_id == tender_id)
    if job_id:
        conditions.append(ExecutionLog.job_id == job_id)
    if connector:
        conditions.append(ExecutionLog.connector == connector)
    if event:
        conditions.append(ExecutionLog.event == event)
    if level:
        conditions.append(ExecutionLog.level == level.upper())

    count_query = select(func.count(ExecutionLog.id))
    query = select(ExecutionLog)
    if conditions:
        count_query = count_query.where(*conditions)
        query = query.where(*conditions)

    total = (await session.execute(count_query)).scalar_one()
    rows = (
        (
            await session.execute(
                query.order_by(desc(ExecutionLog.ts))
                .offset(params.offset)
                .limit(params.page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [
        {
            "id": row.id,
            "ts": row.ts.isoformat(),
            "level": row.level,
            "event": row.event,
            "stage": row.stage,
            "connector": row.connector,
            "tender_id": str(row.tender_id) if row.tender_id else None,
            "job_id": str(row.job_id) if row.job_id else None,
            "message": row.message,
            "duration_ms": row.duration_ms,
            "error_type": row.error_type,
            "context": row.context,
        }
        for row in rows
    ]
    return Page.build(items, total, params)
