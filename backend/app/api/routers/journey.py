"""The four phase counters, in one round trip.

The dashboard described ingestion and nothing else, so anyone opening the
product for the first time concluded it was a scraper. These are the numbers
that tell the whole story — detection, matching, selection, dossier — and they
live in four different tables.

One endpoint rather than four calls, because the browser should not have to
assemble a narrative out of separate requests that can disagree with each
other by a second.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.core.enums import GeneratedDocumentStatus, ShortlistStatus
from app.db.models.cv import CV
from app.db.models.document import GeneratedDocument
from app.db.models.shortlist import Shortlist
from app.db.models.tender import Tender

router = APIRouter(prefix="/stats", tags=["health"])


@router.get("/journey", summary="The four phases, counted")
async def journey(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    async def count(statement: Any) -> int:
        return int((await session.execute(statement)).scalar_one() or 0)

    tenders = await count(select(func.count()).select_from(Tender))
    scored = await count(
        select(func.count()).select_from(Tender).where(Tender.relevance_score.isnot(None))
    )
    # CVs, shortlists and documents are an organisation's own data; tenders are
    # public notices and are shared. The filter follows that line, here as
    # everywhere else.
    cvs = await count(
        select(func.count()).select_from(CV).where(CV.tenant_id == principal.tenant)
    )
    indexed = await count(
        select(func.count())
        .select_from(CV)
        .where(CV.tenant_id == principal.tenant, CV.extraction_chars > 0)
    )
    shortlists = await count(
        select(func.count())
        .select_from(Shortlist)
        .where(Shortlist.tenant_id == principal.tenant)
    )
    validated = await count(
        select(func.count())
        .select_from(Shortlist)
        .where(
            Shortlist.tenant_id == principal.tenant,
            Shortlist.status == ShortlistStatus.VALIDATED.value,
        )
    )
    documents = await count(
        select(func.count())
        .select_from(GeneratedDocument)
        .where(
            GeneratedDocument.tenant_id == principal.tenant,
            GeneratedDocument.status != GeneratedDocumentStatus.SUPERSEDED.value,
        )
    )
    approved = await count(
        select(func.count())
        .select_from(GeneratedDocument)
        .where(
            GeneratedDocument.tenant_id == principal.tenant,
            GeneratedDocument.status == GeneratedDocumentStatus.APPROVED.value,
        )
    )

    # Each phase carries a total and the subset that got further, because the
    # interesting number is rarely the first one: 532 tenders detected matters
    # less than how many were scored, and a produced document matters less
    # than one a human approved.
    return {
        "phases": [
            {"key": "detection", "label": "Détection", "total": tenders, "done": scored,
             "done_label": "évalués", "href": "/tenders"},
            {"key": "matching", "label": "Matching", "total": cvs, "done": indexed,
             "done_label": "indexés", "href": "/matching/recherche"},
            {"key": "selection", "label": "Sélection", "total": shortlists,
             "done": validated, "done_label": "validées", "href": "/dossiers"},
            {"key": "dossier", "label": "Dossier", "total": documents, "done": approved,
             "done_label": "approuvés", "href": "/dossiers"},
        ]
    }


__all__ = ["router"]
