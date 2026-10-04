"""Reading and judging frozen candidate rankings.

Capturing one lives in :mod:`app.api.routers.matching`, next to the run it
freezes. What is here is everything that happens afterwards: reading a capture
back, recording a decision on a candidate, and locking the selection.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.core.enums import ShortlistDecision
from app.core.logging import get_logger
from app.db.models.document import GeneratedDocument
from app.db.models.shortlist import Shortlist
from app.services.shortlist import (
    ShortlistLocked,
    apply_decision,
    summarise,
    validate_shortlist,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/shortlists", tags=["shortlists"])


class DecisionRequest(BaseModel):
    decision: ShortlistDecision
    #: Why, in the validator's words. Optional in the schema and requested in
    #: the interface: a rejection with a reason is labelled data for Phase 8, a
    #: rejection without one is noise. Making it mandatory here would only
    #: produce a field full of ".".
    note: str | None = Field(default=None, max_length=2000)


class ValidateRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


@router.get("", summary="List frozen shortlists")
async def list_shortlists(
    tender_id: uuid_module.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    query = Shortlist.owned_by(principal.tenant)
    if tender_id is not None:
        query = query.where(Shortlist.tender_id == tender_id)

    counter = (
        select(func.count()).select_from(Shortlist).where(Shortlist.tenant_id == principal.tenant)
    )
    if tender_id is not None:
        counter = counter.where(Shortlist.tender_id == tender_id)
    total = (await session.execute(counter)).scalar_one()

    rows = (
        (
            await session.execute(
                query.order_by(Shortlist.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    # One grouped count rather than one request per row. A listing that makes
    # the browser ask "how many documents?" twenty-five times is a table that
    # takes a second to draw.
    counts: dict[Any, int] = {}
    if rows:
        counted = await session.execute(
            select(GeneratedDocument.shortlist_id, func.count())
            .where(GeneratedDocument.shortlist_id.in_([row.id for row in rows]))
            .where(GeneratedDocument.status != "superseded")
            .group_by(GeneratedDocument.shortlist_id)
        )
        counts = dict(counted.all())

    items = []
    for row in rows:
        payload = summarise(row, include_entries=False)
        payload["documents"] = counts.get(row.id, 0)
        items.append(payload)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        # Entries are omitted from a listing on purpose: a capture holds up to
        # twenty-eight candidates with their evidence passages, and a page of
        # twenty-five of those is megabytes for a screen that shows titles.
        "items": items,
    }


@router.get("/{shortlist_id}", summary="Read one shortlist with its candidates")
async def get_shortlist(
    shortlist_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    row = await _load(session, shortlist_id, principal.tenant)
    return summarise(row)


@router.patch("/{shortlist_id}/entries/{entry_id}", summary="Retain or reject a candidate")
async def decide(
    shortlist_id: uuid_module.UUID,
    entry_id: uuid_module.UUID,
    payload: DecisionRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    row = await _load(session, shortlist_id, principal.tenant)
    entry = next((e for e in row.entries if e.id == entry_id), None)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Candidate not found.")

    try:
        apply_decision(
            entry,
            decision=payload.decision,
            actor=principal.identity,
            note=payload.note,
        )
    except ShortlistLocked as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.flush()
    logger.info(
        "api.shortlist_decision",
        shortlist_uuid=str(shortlist_id),
        decision=payload.decision.value,
        vetoed=entry.vetoed,
        actor=principal.identity,
    )
    return summarise(row)


@router.post("/{shortlist_id}/validate", summary="Lock the selection")
async def validate(
    shortlist_id: uuid_module.UUID,
    payload: ValidateRequest | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Past this point the selection is an input to generation, not a draft.

    Nothing is auto-decided here. Candidates left ``pending`` stay pending —
    marking them rejected would record a judgement against profiles the
    validator never opened, and "not selected" is not the same finding as
    "turned down".
    """
    row = await _load(session, shortlist_id, principal.tenant)
    if payload is not None and payload.note is not None:
        row.note = payload.note

    try:
        validate_shortlist(row, actor=principal.identity)
    except ShortlistLocked as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await session.flush()
    return summarise(row)


async def _load(
    session: AsyncSession, shortlist_id: uuid_module.UUID, tenant: str
) -> Shortlist:
    row = (
        await session.execute(Shortlist.owned_by(tenant).where(Shortlist.id == shortlist_id))
    ).scalar_one_or_none()
    if row is None:
        # Deliberately the same answer as "belongs to another organisation".
        # A 403 would confirm the shortlist exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Shortlist not found.")
    return row


__all__ = ["router"]
