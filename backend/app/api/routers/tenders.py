"""Tender listing and detail — what the dashboard reads."""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import Principal, get_session, pagination, require_principal
from app.core.enums import RelevanceBand, TenderStatus
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.db.models.tender import Tender, TenderDocument, TenderScore
from app.schemas.common import Page, PaginationParams
from app.schemas.tender import ScoreBreakdown, TenderDetail, TenderSummary

logger = get_logger(__name__)
router = APIRouter(prefix="/tenders", tags=["tenders"])

#: Columns a client may sort by. An allowlist, not reflection over the model:
#: accepting an arbitrary column name is how an ORDER BY becomes an injection
#: vector or a full-table sort on an unindexed column.
_SORTABLE = {
    "created_at": Tender.created_at,
    "deadline": Tender.deadline,
    "publication_date": Tender.publication_date,
    "relevance_score": Tender.relevance_score,
    "estimated_budget": Tender.estimated_budget,
    "title": Tender.title,
}


@router.get("", response_model=Page[TenderSummary], summary="List tenders")
async def list_tenders(
    session: AsyncSession = Depends(get_session),
    params: PaginationParams = Depends(pagination),
    q: str | None = Query(default=None, description="Free text over title, buyer and reference."),
    connectors: list[str] = Query(default_factory=list),
    countries: list[str] = Query(default_factory=list),
    sectors: list[str] = Query(default_factory=list),
    bands: list[RelevanceBand] = Query(default_factory=list),
    min_score: float | None = Query(default=None, ge=0, le=1),
    only_open: bool = Query(default=True),
    sort: str = Query(default="-relevance_score"),
    _: Principal = Depends(require_principal),
) -> Page[TenderSummary]:
    query = select(Tender)
    count_query = select(func.count(Tender.id))
    conditions: list[Any] = []

    # Out-of-scope tenders are stored for auditability but are never part of
    # the working set.
    conditions.append(Tender.relevance_band != RelevanceBand.OUT_OF_SCOPE.value)

    if q:
        pattern = f"%{q.strip()}%"
        conditions.append(
            or_(
                Tender.title.ilike(pattern),
                Tender.buyer.ilike(pattern),
                Tender.reference.ilike(pattern),
            )
        )
    if connectors:
        conditions.append(Tender.source_key.in_(connectors))
    if countries:
        conditions.append(Tender.country.in_(countries))
    if sectors:
        conditions.append(Tender.sector.in_(sectors))
    if bands:
        conditions.append(Tender.relevance_band.in_([b.value for b in bands]))
    if min_score is not None:
        conditions.append(Tender.relevance_score >= min_score)
    if only_open:
        conditions.append(
            or_(Tender.deadline.is_(None), Tender.deadline >= utc_now())
        )
        conditions.append(
            Tender.status.notin_([TenderStatus.CANCELLED.value, TenderStatus.AWARDED.value])
        )

    if conditions:
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)

    total = (await session.execute(count_query)).scalar_one()

    descending = sort.startswith("-")
    column = _SORTABLE.get(sort.lstrip("-"))
    if column is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot sort by '{sort}'. Sortable fields: {', '.join(sorted(_SORTABLE))}.",
        )
    # NULLS LAST on both directions: unscored tenders belong at the bottom of a
    # relevance ranking, never at the top of it.
    ordering = desc(column).nulls_last() if descending else asc(column).nulls_last()

    rows = (
        (
            await session.execute(
                query.order_by(ordering, desc(Tender.created_at))
                .offset(params.offset)
                .limit(params.page_size)
            )
        )
        .scalars()
        .all()
    )
    return Page.build([TenderSummary.model_validate(row) for row in rows], total, params)


@router.get("/{tender_id}", response_model=TenderDetail, summary="Get a tender")
async def get_tender(
    tender_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_principal),
) -> TenderDetail:
    row = (
        await session.execute(
            select(Tender).options(selectinload(Tender.documents)).where(Tender.id == tender_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tender not found.")

    detail = TenderDetail.model_validate(row)

    latest = (
        await session.execute(
            select(TenderScore)
            .where(TenderScore.tender_id == tender_id)
            .order_by(desc(TenderScore.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None:
        detail.latest_score = ScoreBreakdown(
            profile_version=latest.profile_version,
            score=latest.score,
            band=latest.band,
            breakdown=latest.breakdown,
            weights=latest.weights,
            computed_at=latest.created_at,
        )
    return detail


@router.get(
    "/{tender_id}/download",
    summary="Get a time-limited download link for the original document",
)
async def download_tender(
    tender_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Return a presigned URL.

    The API never proxies file bytes: streaming a 25 MB PDF through the request
    handler would occupy a worker for the whole transfer, which is exactly the
    kind of blocking this architecture refuses.
    """
    import anyio

    row = (await session.execute(select(Tender).where(Tender.id == tender_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tender not found.")
    if not row.storage_key:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="No original document is stored for this tender.",
        )

    from app.services.storage import get_storage

    storage = get_storage()
    url = await anyio.to_thread.run_sync(lambda: storage.presigned_url(row.storage_key))
    logger.info("api.download_link_issued", tender_uuid=str(tender_id), actor=principal.identity)
    return {
        "url": url,
        "expires_in_seconds": storage.presigned_ttl,
        "filename": row.original_filename,
        "content_type": row.content_type,
    }


@router.get(
    "/{tender_id}/documents/{document_id}/download",
    summary="Get a time-limited download link for one attachment",
)
async def download_tender_document(
    tender_id: uuid_module.UUID,
    document_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Return a presigned URL for one of the tender's attachments.

    Same rule as ``/{tender_id}/download``: the API never proxies file bytes.
    A ``404`` here distinguishes two cases that look identical from a client's
    point of view but mean different things to an operator — the attachment
    row does not exist versus it exists but has not been fetched yet (still
    ``pending`` or the download failed).
    """
    import anyio

    row = (
        await session.execute(
            select(TenderDocument).where(
                TenderDocument.id == document_id, TenderDocument.tender_id == tender_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found.")
    if not row.storage_key:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="This document has not been downloaded yet.",
        )

    from app.services.storage import get_storage

    storage = get_storage()
    url = await anyio.to_thread.run_sync(lambda: storage.presigned_url(row.storage_key))
    logger.info(
        "api.document_download_link_issued",
        tender_uuid=str(tender_id),
        document_id=str(document_id),
        actor=principal.identity,
    )
    return {
        "url": url,
        "expires_in_seconds": storage.presigned_ttl,
        "filename": row.name,
        "content_type": row.content_type,
    }


@router.get("/{tender_id}/scores", response_model=list[ScoreBreakdown], summary="Scoring history")
async def tender_scores(
    tender_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_principal),
) -> list[ScoreBreakdown]:
    """Every scoring execution for this tender, newest first.

    Retained so a past ranking stays explainable after the weights change.
    """
    rows = (
        (
            await session.execute(
                select(TenderScore)
                .where(TenderScore.tender_id == tender_id)
                .order_by(desc(TenderScore.created_at))
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return [
        ScoreBreakdown(
            profile_version=row.profile_version,
            score=row.score,
            band=row.band,
            breakdown=row.breakdown,
            weights=row.weights,
            computed_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/stats/overview", summary="Dashboard counters", include_in_schema=True)
async def stats_overview(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Aggregates for the dashboard header."""
    from datetime import timedelta

    now = utc_now()
    band_rows = (
        await session.execute(
            select(Tender.relevance_band, func.count(Tender.id)).group_by(Tender.relevance_band)
        )
    ).all()
    source_rows = (
        await session.execute(
            select(Tender.source_key, func.count(Tender.id)).group_by(Tender.source_key)
        )
    ).all()

    total = (await session.execute(select(func.count(Tender.id)))).scalar_one()
    # The number a bid manager acts on is "what can I still bid on", not "how
    # many notices have we ever collected". Both are reported: the archive is
    # what feeds duplicate detection and the win/loss history, so it must not
    # be hidden — but it must not be the headline either.
    still_open = (
        await session.execute(
            select(func.count(Tender.id)).where(
                or_(Tender.deadline.is_(None), Tender.deadline >= now),
                Tender.status.notin_(
                    [TenderStatus.CLOSED.value, TenderStatus.CANCELLED.value]
                ),
            )
        )
    ).scalar_one()
    last_24h = (
        await session.execute(
            select(func.count(Tender.id)).where(Tender.created_at >= now - timedelta(days=1))
        )
    ).scalar_one()
    closing_soon = (
        await session.execute(
            select(func.count(Tender.id)).where(
                Tender.deadline.isnot(None),
                Tender.deadline >= now,
                Tender.deadline <= now + timedelta(days=7),
                Tender.relevance_band.in_(
                    [RelevanceBand.HIGHLY_RELEVANT.value, RelevanceBand.RELEVANT.value]
                ),
            )
        )
    ).scalar_one()

    from app.services.scoring import get_scoring_engine

    return {
        "total_tenders": total,
        "open_tenders": still_open,
        "archived_tenders": total - still_open,
        "ingested_last_24h": last_24h,
        "relevant_closing_within_7_days": closing_soon,
        "by_band": dict(band_rows),
        "by_source": dict(source_rows),
        "band_metadata": get_scoring_engine().band_metadata(),
    }
