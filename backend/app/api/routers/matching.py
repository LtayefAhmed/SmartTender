"""Ranking candidates against a tender, from the interface.

The work happens in the ``ai`` worker, never here. Matching needs the embedding
model, and the model is 470 MB of resident memory: loading it in the API would
put a copy in every request process, which is the mistake that took the host
down when indexing ran on an eight-process queue. One worker owns the model;
the API asks it a question and waits for the answer.

Waiting rather than polling is a deliberate simplification. A run costs a few
seconds — fifteen encodings and fifteen vector queries — which is short enough
for a button and a spinner, and a result nobody stores is a result nobody has
to invalidate when a CV is re-imported. If it ever grows past that, the job
table the scraping module already uses is the pattern to follow.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.core.logging import get_logger
from app.db.models.tender import Tender
from app.workers.celery_app import TASK_PRIORITY

logger = get_logger(__name__)
router = APIRouter(prefix="/tenders", tags=["matching"])

#: A run is a handful of seconds. Beyond this something is wrong — the model
#: failed to load, or the vector store is unreachable — and a caller left
#: hanging learns less than one told plainly that it timed out.
_TIMEOUT_SECONDS = 90


class CaptureRequest(BaseModel):
    label: str | None = Field(default=None, max_length=255)
    limit: int = Field(default=20, ge=1, le=100)
    requirements: int = Field(default=15, ge=3, le=40)


@router.get("/{tender_id}/candidates", summary="Rank CVs against this tender")
async def rank_candidates(
    tender_id: uuid_module.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    requirements: int = Query(default=15, ge=3, le=40),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Return the organisation's best candidates for one tender, with evidence.

    Ranking is per tender and never absolute: a profile that is excellent for a
    SAP migration is the wrong person for a Symfony rewrite, so there is no
    such thing as "the best CV" to cache.
    """
    outcome = await _run_matching(
        session,
        tender_id=tender_id,
        tenant=principal.tenant,
        limit=limit,
        requirements=requirements,
    )
    logger.info(
        "api.matching_completed",
        tender_uuid=str(tender_id),
        candidates=len(outcome.get("candidates", [])),
        actor=principal.identity,
    )
    return outcome


@router.post(
    "/{tender_id}/shortlist",
    status_code=status.HTTP_201_CREATED,
    summary="Freeze the ranking into a shortlist a human can judge",
)
async def capture_shortlist(
    tender_id: uuid_module.UUID,
    payload: CaptureRequest | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Run the ranking and store it, with the question it answered.

    This re-runs rather than accepting the list the browser is showing, and the
    difference matters. A capture is an audit record: it has to be something
    the server computed, not something a client posted back. What was on screen
    was a preview; the stored capture is the authoritative act, and the
    interface reloads from it immediately so the two never diverge unnoticed.

    Ranking is deterministic given the same index and weights, so in practice
    the two agree. When they do not, the capture is the one that is true.
    """
    request = payload or CaptureRequest()
    outcome = await _run_matching(
        session,
        tender_id=tender_id,
        tenant=principal.tenant,
        limit=request.limit,
        requirements=request.requirements,
    )
    if outcome.get("status") != "ok":
        # A notice with no readable text produces no ranking, and freezing an
        # empty one would create a record that looks like "nobody fits" when it
        # means "we could not read the dossier".
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=outcome.get("message") or "This tender cannot be ranked yet.",
        )

    from app.services.shortlist import build_shortlist, summarise

    shortlist = build_shortlist(
        outcome,
        tenant=principal.tenant,
        created_by=principal.identity,
        label=request.label,
    )
    session.add(shortlist)
    await session.flush()

    logger.info(
        "api.shortlist_captured",
        tender_uuid=str(tender_id),
        shortlist_uuid=str(shortlist.id),
        entries=len(shortlist.entries),
        actor=principal.identity,
    )
    return summarise(shortlist)


async def _run_matching(
    session: AsyncSession,
    *,
    tender_id: uuid_module.UUID,
    tenant: str,
    limit: int,
    requirements: int,
) -> dict[str, Any]:
    """One ranking run on the ``ai`` worker, or a plain failure."""
    row = (
        await session.execute(
            select(Tender.id, Tender.title, Tender.extraction_chars).where(
                Tender.id == tender_id
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Tender not found.")

    from app.workers.tasks.matching import rank_tender_candidates

    task = rank_tender_candidates.apply_async(
        kwargs={
            "tender_id": str(tender_id),
            "tenant": tenant,
            "limit": limit,
            "requirement_limit": requirements,
        },
        queue="ai",
        priority=TASK_PRIORITY["interactive"],
    )

    try:
        # Blocking on a Celery result blocks a thread, not the event loop.
        return await anyio.to_thread.run_sync(
            lambda: task.get(timeout=_TIMEOUT_SECONDS, propagate=True)
        )
    except Exception as exc:
        logger.warning(
            "api.matching_failed", tender_uuid=str(tender_id), error=str(exc)[:200]
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Matching is unavailable. The embedding worker may be starting "
                "or the vector index may be unreachable."
            ),
        ) from exc
