"""Approving a produced document — the parcours' second human lock.

Nothing leaves the platform without an explicit, traced decision. Three
outcomes, as the parcours sets out: approve, send back for changes, refuse.

Two things make approval more than a status change, and both are easy to get
wrong.

**The watermark is inside the file.** "GÉNÉRÉ — EN ATTENTE DE VALIDATION" is
rendered into the .docx, not attached to the row. Flipping a column would
leave a document that says "awaiting validation" across the top of an approved
submission. So approval re-renders the same template, with the same context,
from the same stored rows — and only the watermark changes.

**Approval is what fills the referential.** An approved dossier is the only
thing the platform has that a person stood behind, so it is the only thing
that may be remembered. Indexing happens here and nowhere else.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.core.enums import GeneratedDocumentStatus, TemplateKind
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.db.models.cv import CV
from app.db.models.document import GeneratedDocument
from app.db.models.shortlist import Shortlist, ShortlistEntry
from app.db.models.template import DocumentTemplate
from app.db.models.tender import Tender

logger = get_logger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])


class DecisionRequest(BaseModel):
    #: Requested on a refusal, optional in the schema. Making it mandatory
    #: would fill the column with "." — the same lesson the shortlist's
    #: decision note taught.
    note: str | None = Field(default=None, max_length=2000)


@router.post("/{document_id}/approve", summary="Approve a document and remove its watermark")
async def approve(
    document_id: uuid_module.UUID,
    payload: DecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Sign off a document, re-render it clean, and commit it to memory."""
    row = await _load(session, document_id, principal.tenant)

    if row.status == GeneratedDocumentStatus.APPROVED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Ce document est déjà approuvé.")
    if row.status == GeneratedDocumentStatus.SUPERSEDED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "Cette version a été remplacée par une plus récente. "
                "Approuvez la version courante."
            ),
        )

    clean, text = await _rerender_without_watermark(session, row, principal.tenant)
    if clean is not None:
        from app.services.storage import get_storage

        storage = get_storage()
        await anyio.to_thread.run_sync(
            lambda: storage.put_bytes(
                row.storage_key,
                clean,
                content_type=row.content_type,
                metadata={"document-id": str(row.id), "approved": "true"},
            )
        )
        row.size_bytes = len(clean)
        row.watermarked = False
    else:
        # Re-rendering failed — a retired template, a deleted CV. The approval
        # still stands; what cannot be done is stripping the mark, and saying
        # so beats silently shipping a document stamped "awaiting validation".
        logger.warning("approval.rerender_failed", document_uuid=str(document_id))

    row.status = GeneratedDocumentStatus.APPROVED.value
    row.approved_by = principal.identity
    row.approved_at = utc_now()
    if payload is not None and payload.note:
        row.decision_note = payload.note
    await session.flush()

    passages = 0
    if text:
        # Capitalisation. Only approved text enters the referential: a draft
        # is an opinion, an approved dossier is something a person signed.
        from app.services.memory import index_response

        passages = await anyio.to_thread.run_sync(
            lambda: index_response(
                document_id=str(row.id),
                text=text,
                tenant=principal.tenant,
                kind=row.kind,
                tender_title=_tender_title(row),
                label=row.label,
            )
        )

    logger.info(
        "api.document_approved",
        document_uuid=str(document_id),
        kind=row.kind,
        watermark_removed=not row.watermarked,
        indexed_passages=passages,
        actor=principal.identity,
    )
    return {
        "id": str(row.id),
        "status": row.status,
        "watermarked": row.watermarked,
        "approved_by": row.approved_by,
        "indexed_passages": passages,
    }


@router.post("/{document_id}/reject", summary="Send a document back, with a reason")
async def reject(
    document_id: uuid_module.UUID,
    payload: DecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Refuse a document.

    The row is kept and marked superseded rather than deleted: a version
    somebody reviewed and turned down is part of the trail, and the reason is
    what the learning loop will read.
    """
    row = await _load(session, document_id, principal.tenant)
    if row.status == GeneratedDocumentStatus.APPROVED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Ce document est approuvé. Produisez une nouvelle version pour revenir dessus.",
        )

    row.status = GeneratedDocumentStatus.SUPERSEDED.value
    row.decision_note = (payload.note if payload else None) or row.decision_note
    row.approved_by = None
    await session.flush()

    logger.info(
        "api.document_rejected",
        document_uuid=str(document_id),
        has_reason=bool(row.decision_note),
        actor=principal.identity,
    )
    return {"id": str(row.id), "status": row.status, "decision_note": row.decision_note}


# ---------------------------------------------------------------------------
async def _rerender_without_watermark(
    session: AsyncSession, row: GeneratedDocument, tenant: str
) -> tuple[bytes | None, str]:
    """Rebuild the same document with ``approved=True``.

    Returns the bytes and the plain text, the second for the referential. Both
    are best-effort: a template that was retired or a CV that was deleted at a
    candidate's request leaves the approval valid and the watermark in place.
    """
    template = (
        await session.get(DocumentTemplate, row.template_id) if row.template_id else None
    )
    if template is None:
        return None, ""

    from app.services.storage import get_storage

    storage = get_storage()
    try:
        template_bytes = await anyio.to_thread.run_sync(
            lambda: storage.get_bytes(template.storage_key)
        )
    except Exception as exc:
        logger.warning("approval.template_unreadable", error=str(exc)[:200])
        return None, ""

    shortlist = await session.get(Shortlist, row.shortlist_id) if row.shortlist_id else None
    tender = await session.get(Tender, row.tender_id) if row.tender_id else None

    if row.kind == TemplateKind.COMPLIANCE_MATRIX.value:
        content = await _rerender_matrix(session, row, template_bytes, shortlist, tender)
    else:
        content = await _rerender_profile(session, row, template_bytes, shortlist, tender)

    return content, _plain_text(content) if content else ""


async def _rerender_profile(
    session: AsyncSession,
    row: GeneratedDocument,
    template_bytes: bytes,
    shortlist: Shortlist | None,
    tender: Tender | None,
) -> bytes | None:
    """A CV or an expert sheet, rebuilt from the rows it came from."""
    cv = await session.get(CV, row.cv_id) if row.cv_id else None
    entry = await session.get(ShortlistEntry, row.entry_id) if row.entry_id else None
    if cv is None:
        return None

    from app.services.generation import GenerationRefused, generate_cv

    try:
        # No adapter: approval reproduces the document that was reviewed, and
        # running the model again could produce different wording than the one
        # a person just signed.
        document = await anyio.to_thread.run_sync(
            lambda: generate_cv(
                template_bytes=template_bytes,
                cv=cv,
                entry=entry,
                shortlist=shortlist,
                tender=tender,
                approved=True,
            )
        )
    except GenerationRefused as exc:
        logger.warning("approval.rerender_refused", error=str(exc)[:200])
        return None
    return document.content


async def _rerender_matrix(
    session: AsyncSession,
    row: GeneratedDocument,
    template_bytes: bytes,
    shortlist: Shortlist | None,
    tender: Tender | None,
) -> bytes | None:
    if shortlist is None:
        return None

    from app.core.enums import ShortlistDecision
    from app.services.compliance import build_matrix
    from app.services.templates import TemplateError, render_template

    retained = [
        entry
        for entry in shortlist.entries
        if entry.decision == ShortlistDecision.RETAINED.value
    ]
    matrix = build_matrix(shortlist, retained)
    context = {
        **matrix.to_dict(),
        "equipe": matrix.profils,
        "mission_titre": (getattr(tender, "title", None) or shortlist.tender_title or ""),
        "mission_acheteur": getattr(tender, "buyer", None) or "",
        "mission_reference": getattr(tender, "reference", None) or "",
        "mission_pays": getattr(tender, "country", None) or "",
        "mission_echeance": (
            tender.deadline.strftime("%d/%m/%Y")
            if tender is not None and getattr(tender, "deadline", None)
            else ""
        ),
        "genere_le": utc_now().strftime("%d/%m/%Y"),
        "filigrane": "",
    }
    try:
        return await anyio.to_thread.run_sync(
            lambda: render_template(template_bytes, context)
        )
    except TemplateError as exc:
        logger.warning("approval.matrix_rerender_failed", error=str(exc)[:200])
        return None


def _plain_text(content: bytes) -> str:
    """The document's words, for the referential.

    The .docx is the artefact; what is remembered is its prose.
    """
    import io

    try:
        from docx import Document

        document = Document(io.BytesIO(content))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            parts.extend(
                cell.text for line in table.rows for cell in line.cells if cell.text.strip()
            )
        return "\n".join(parts)
    except Exception:
        return ""


def _tender_title(row: GeneratedDocument) -> str | None:
    return row.label if row.kind == TemplateKind.COMPLIANCE_MATRIX.value else None


async def _load(
    session: AsyncSession, document_id: uuid_module.UUID, tenant: str
) -> GeneratedDocument:
    row = (
        await session.execute(
            GeneratedDocument.owned_by(tenant).where(GeneratedDocument.id == document_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document introuvable.")
    return row


__all__ = ["router"]
