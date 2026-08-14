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
    "/{tender_id}/documents/{document_id}/download",
    summary="Get a time-limited download link for one attachment",
)
async def download_document(
    tender_id: uuid_module.UUID,
    document_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Return a presigned URL for a stored attachment.

    Downloading the cahier des charges was previously impossible from the
    interface: the files were fetched and stored, the screen showed them, and
    the only way to read one was to open the object store by hand. Collecting a
    document nobody can reach is close to not collecting it.
    """
    import anyio

    from app.db.models.tender import TenderDocument

    document = (
        await session.execute(
            select(TenderDocument).where(
                TenderDocument.id == document_id,
                TenderDocument.tender_id == tender_id,
            )
        )
    ).scalar_one_or_none()

    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Attachment not found.")
    if document.status != "stored" or not document.storage_key:
        # The row exists but the bytes never arrived. Saying which is what lets
        # someone decide between retrying and opening the source portal.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "message": "This attachment was never stored.",
                "status": document.status,
                "reason": document.error_message,
                "source_url": document.source_url,
            },
        )

    from app.services.storage import get_storage

    storage = get_storage()
    url = await anyio.to_thread.run_sync(lambda: storage.presigned_url(document.storage_key))
    logger.info(
        "api.document_link_issued",
        tender_uuid=str(tender_id),
        document_uuid=str(document_id),
        actor=principal.identity,
    )
    return {
        "url": url,
        "name": document.name,
        "content_type": document.content_type,
        "size_bytes": document.size_bytes,
    }


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


#: Below this, a tender's stored text is an abstract rather than a
#: specification. Chosen from measurement: J360's Tunisian notices land around
#: 500 characters with no attachment, while one consultation with its CCTP
#: reached 441 000. Anything under a few thousand characters cannot support
#: matching a CV against required skills.
_THIN_TEXT_CHARS = 3_000


@router.get("/stats/completeness", summary="What is missing from the corpus")
async def stats_completeness(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Report what was *not* collected.

    Every other counter in this API answers "what do we have". None of them
    answers "what are we missing", and that is the question that matters when
    the corpus feeds CV matching: a tender whose CCTP never downloaded still
    appears, still scores, and silently contributes nothing to the match. The
    losses this endpoint surfaces were all found by hand — a document cap that
    dropped the règlement de consultation, archives stored but never opened,
    links inside a publication that nothing followed. Each was invisible
    because nothing failed.
    """
    from app.db.models.tender import TenderDocument

    in_scope = Tender.relevance_band != RelevanceBand.OUT_OF_SCOPE.value

    total = (await session.execute(select(func.count(Tender.id)).where(in_scope))).scalar_one()

    document_status = dict(
        (
            await session.execute(
                select(TenderDocument.status, func.count(TenderDocument.id)).group_by(
                    TenderDocument.status
                )
            )
        ).all()
    )

    # Grouped so a systematic cause — an expired signature, a portal requiring
    # a login — is visible as one large number instead of many single failures.
    failure_rows = (
        await session.execute(
            select(TenderDocument.error_message, func.count(TenderDocument.id))
            .where(TenderDocument.status == "failed")
            .group_by(TenderDocument.error_message)
            .order_by(desc(func.count(TenderDocument.id)))
            .limit(10)
        )
    ).all()

    with_documents = (
        await session.execute(
            select(func.count(func.distinct(TenderDocument.tender_id))).where(
                TenderDocument.status == "stored"
            )
        )
    ).scalar_one()

    # Matchable text is the publication *plus* the attachments, not the
    # attachments alone. Counting only `extraction_chars` reported a Moroccan
    # notice carrying an 8 262-character publication as having no text at all,
    # and made whole countries look empty when they were merely dossier-less.
    matchable = func.coalesce(func.length(Tender.description), 0) + Tender.extraction_chars

    no_text = (
        await session.execute(select(func.count(Tender.id)).where(in_scope, matchable == 0))
    ).scalar_one()
    thin_text = (
        await session.execute(
            select(func.count(Tender.id)).where(
                in_scope, matchable > 0, matchable < _THIN_TEXT_CHARS
            )
        )
    ).scalar_one()
    # Reported separately because the two carry different value: a publication
    # states the object and the criteria, a dossier states the requirements.
    # Only the second can support matching a CV against required skills.
    with_dossier = (
        await session.execute(
            select(func.count(Tender.id)).where(in_scope, Tender.extraction_chars > 0)
        )
    ).scalar_one()
    # Truncation is the quietest loss of all: the tender looks complete, the
    # character count looks impressive, and the tail of the dossier is simply
    # gone. It is inferred rather than recorded — `clean_extracted_text` cuts
    # at the cap and trims back to a word boundary, so a tender landing within
    # a few characters of the ceiling was cut.
    from app.core.config import get_settings

    cap = get_settings().extraction.max_chars_per_tender
    truncated = (
        await session.execute(
            select(func.count(Tender.id)).where(in_scope, Tender.extraction_chars >= cap - 200)
        )
    ).scalar_one()

    extraction_status = dict(
        (
            await session.execute(
                select(Tender.extraction_status, func.count(Tender.id))
                .where(in_scope)
                .group_by(Tender.extraction_status)
            )
        ).all()
    )
    extraction_errors = (
        await session.execute(
            select(Tender.extraction_error, func.count(Tender.id))
            .where(in_scope, Tender.extraction_error.isnot(None))
            .group_by(Tender.extraction_error)
            .order_by(desc(func.count(Tender.id)))
            .limit(10)
        )
    ).all()

    return {
        "tenders_in_scope": total,
        "tenders_without_stored_document": total - with_documents,
        "tenders_without_text": no_text,
        "tenders_with_dossier_text": with_dossier,
        "tenders_with_thin_text": thin_text,
        "thin_text_threshold_chars": _THIN_TEXT_CHARS,
        "tenders_with_truncated_text": truncated,
        "text_cap_chars": cap,
        "documents_by_status": document_status,
        "document_failures": [
            {"reason": reason or "unspecified", "count": count} for reason, count in failure_rows
        ],
        "extraction_by_status": extraction_status,
        "extraction_errors": [
            {"reason": reason, "count": count} for reason, count in extraction_errors
        ],
    }


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
