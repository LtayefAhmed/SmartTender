"""Exporting a dossier, and the trail that explains it.

The parcours' last phase: the finished bid leaves the platform as one package,
by a signed link with a limited life, and everything that produced it stays
queryable from end to end.

Two decisions shape what "finished" means here.

**Only approved documents are exported.** A dossier of drafts is not a dossier;
it is work in progress with a watermark across every page. The export refuses
rather than quietly packaging whatever happens to exist, because a zip that
sometimes contains drafts is a zip nobody can trust without opening.

**The manifest travels inside the package.** Which template version produced
each file, who approved it and when, whether a model touched its wording. A
document that leaves the platform without that is a document whose provenance
lives only in a database the recipient cannot read.
"""

from __future__ import annotations

import io
import uuid as uuid_module
import zipfile
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.core.enums import GeneratedDocumentStatus
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.db.models.document import GeneratedDocument
from app.db.models.log import ExecutionLog
from app.db.models.shortlist import Shortlist
from app.services.audit import build_event

logger = get_logger(__name__)
router = APIRouter(tags=["documents"])


@router.post(
    "/shortlists/{shortlist_id}/export",
    status_code=status.HTTP_201_CREATED,
    summary="Package the approved dossier and return a signed link",
)
async def export_dossier(
    shortlist_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    shortlist = (
        await session.execute(
            Shortlist.owned_by(principal.tenant).where(Shortlist.id == shortlist_id)
        )
    ).scalar_one_or_none()
    if shortlist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Sélection introuvable.")

    documents = (
        (
            await session.execute(
                GeneratedDocument.owned_by(principal.tenant)
                .where(GeneratedDocument.shortlist_id == shortlist_id)
                .where(GeneratedDocument.status == GeneratedDocumentStatus.APPROVED.value)
                .order_by(GeneratedDocument.kind, GeneratedDocument.label)
            )
        )
        .scalars()
        .all()
    )
    if not documents:
        pending = (
            await session.execute(
                select(GeneratedDocument.id)
                .where(GeneratedDocument.shortlist_id == shortlist_id)
                .where(GeneratedDocument.status == GeneratedDocumentStatus.DRAFT.value)
            )
        ).all()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Aucun document approuvé pour ce dossier. {len(pending)} brouillon(s) "
                "en attente : un dossier de brouillons porte le filigrane « en attente "
                "de validation » sur chaque page."
            ),
        )

    from app.services.storage import get_storage

    storage = get_storage()
    buffer = io.BytesIO()
    manifest: list[str] = [
        "DOSSIER DE RÉPONSE — MANIFESTE",
        "",
        f"Appel d'offres : {shortlist.tender_title or '—'}",
        f"Sélection validée par : {shortlist.validated_by or '—'}",
        f"Export : {utc_now().strftime('%d/%m/%Y %H:%M')} UTC par {principal.identity}",
        "",
        "PIÈCES",
    ]

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for document in documents:
            try:
                content = await anyio.to_thread.run_sync(
                    lambda key=document.storage_key: storage.get_bytes(key)
                )
            except Exception as exc:
                # Reported, never silently dropped: a dossier short of one
                # piece is something a bid manager has to know before sending.
                logger.warning(
                    "export.piece_unreadable", document_uuid=str(document.id),
                    error=str(exc)[:200],
                )
                manifest.append(f"  [MANQUANTE] {document.filename}")
                continue

            archive.writestr(f"{document.kind}/{document.filename}", content)
            touched = bool((document.adaptation or {}).get("llm_used"))
            written = "modèle + contrôle automatique" if touched else "déterministe"
            manifest.append(
                f"  {document.filename}"
                f"\n      type        : {document.kind}"
                f"\n      gabarit     : {document.template_key} v{document.template_version}"
                f"\n      approuvé par: {document.approved_by or '—'}"
                f"\n      rédaction   : {written}"
            )

        archive.writestr("MANIFESTE.txt", "\n".join(manifest).encode("utf-8"))

    package = buffer.getvalue()
    export_id = uuid_module.uuid4()
    reference = _slug(shortlist.tender_title or "") or "dossier"
    filename = f"dossier_{reference}.zip"
    stored = await anyio.to_thread.run_sync(
        lambda: storage.put_bytes(
            storage.build_key(str(export_id), filename, prefix="exports"),
            package,
            content_type="application/zip",
            metadata={"shortlist-id": str(shortlist_id)},
        )
    )
    url = await anyio.to_thread.run_sync(lambda: storage.presigned_url(stored.key))

    entry = build_event(
        "dossier.exported",
        actor=principal.identity,
        message=f"{len(documents)} pièce(s) exportée(s)",
        context={
            "shortlist_id": str(shortlist_id),
            "documents": [str(d.id) for d in documents],
            "bytes": len(package),
        },
    )
    if entry is not None:
        session.add(entry)
    await session.flush()

    logger.info(
        "api.dossier_exported",
        shortlist_uuid=str(shortlist_id),
        pieces=len(documents),
        bytes=len(package),
        actor=principal.identity,
    )
    return {
        "url": url,
        "filename": filename,
        "pieces": len(documents),
        "size_bytes": len(package),
    }


@router.get("/shortlists/{shortlist_id}/audit", summary="Everything that produced this dossier")
async def dossier_audit(
    shortlist_id: uuid_module.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """The trail, read back end to end.

    Assembled from the rows themselves rather than from the log alone: the
    decisions *are* the data — who validated the selection, who approved each
    document, which template version and which model produced it. A log line
    can be lost; a row that records a decision is the decision.
    """
    shortlist = (
        await session.execute(
            Shortlist.owned_by(principal.tenant).where(Shortlist.id == shortlist_id)
        )
    ).scalar_one_or_none()
    if shortlist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Sélection introuvable.")

    documents = (
        (
            await session.execute(
                GeneratedDocument.owned_by(principal.tenant)
                .where(GeneratedDocument.shortlist_id == shortlist_id)
                .order_by(GeneratedDocument.created_at)
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await session.execute(
                select(ExecutionLog)
                .where(ExecutionLog.event.like("dossier.%"))
                .order_by(ExecutionLog.ts.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return {
        "selection": {
            "statut": shortlist.status,
            "figee_par": shortlist.created_by,
            "figee_le": shortlist.created_at.isoformat() if shortlist.created_at else None,
            "validee_par": shortlist.validated_by,
            "validee_le": (
                shortlist.validated_at.isoformat() if shortlist.validated_at else None
            ),
            # The weights the ranking used, frozen with it. A score without its
            # scale is a number nobody can argue with.
            "poids": dict(shortlist.weights or {}),
            "exigences_lues": len(shortlist.requirements or []),
            "retenus": sum(1 for e in shortlist.entries if e.decision == "retained"),
            "ecartes_par_le_verrou": shortlist.vetoed_total,
        },
        "documents": [
            {
                "id": str(d.id),
                "type": d.kind,
                "libelle": d.label,
                "version": d.version,
                "statut": d.status,
                "gabarit": f"{d.template_key} v{d.template_version}",
                "produit_par": d.generated_by,
                "approuve_par": d.approved_by,
                "approuve_le": d.approved_at.isoformat() if d.approved_at else None,
                # What the model did, and what the automatic review found.
                "modele": dict(d.adaptation or {}),
                "controle": dict(d.qa or {}),
                "motif": d.decision_note,
            }
            for d in documents
        ],
        "journal": [
            {
                "quand": event.ts.isoformat() if event.ts else None,
                "quoi": event.event,
                "qui": event.actor,
                "message": event.message,
                "details": event.context,
            }
            for event in events
        ],
    }


def _slug(value: str) -> str:
    import unicodedata

    ascii_only = (
        unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    )
    cleaned = "".join(c if c.isalnum() else "_" for c in ascii_only)
    return "_".join(part for part in cleaned.split("_") if part)[:50]


__all__ = ["router"]
