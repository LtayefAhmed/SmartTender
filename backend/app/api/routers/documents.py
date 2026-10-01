"""Producing the dossier, and reading back what was produced.

This is the step the whole module exists for: a validated selection goes in,
one document per retained profile comes out. Everything before it — detection,
ranking, the frozen shortlist, the template library — was preparation.

Three rules are enforced here rather than left to the interface.

**Only a validated selection produces documents.** An open shortlist is a
working draft; generating from it would put a real person's name on a real
submission before anyone signed off on the list.

**Only retained profiles.** ``pending`` means nobody looked, which is a
different fact from ``rejected``, and neither is a decision to include someone.

**Regeneration versions rather than overwrites.** A document that was already
circulated has to stay explainable, so the previous version is superseded and
kept, not replaced.

Generation runs inline rather than on a worker. Measured end to end at 289 ms
per CV — reading the row, fetching the template from object storage, rendering
— so ten profiles is a button and a spinner, not a job queue. The CPU and I/O
legs go through a thread so the event loop keeps serving.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.core.enums import (
    GeneratedDocumentStatus,
    ShortlistDecision,
    ShortlistStatus,
    TemplateKind,
)
from app.core.logging import get_logger
from app.db.models.cv import CV
from app.db.models.document import GeneratedDocument
from app.db.models.shortlist import Shortlist
from app.db.models.template import DocumentTemplate
from app.db.models.tender import Tender
from app.services.generation import GenerationRefused, generate_cv

logger = get_logger(__name__)
router = APIRouter(tags=["documents"])

#: One reformulation is a Mistral call plus a handful of encodings —
#: measured at 3.6 s for two profiles. Past this, the worker is down or
#: the model is unreachable, and a caller left hanging learns less than
#: one handed the deterministic document.
_ADAPT_TIMEOUT_SECONDS = 120


class GenerateRequest(BaseModel):
    #: Which template to use. Omitted, the active CV template is taken — the
    #: common case, and the one where asking the user to pick would be asking
    #: them to repeat a choice the library already records.
    template_id: uuid_module.UUID | None = None
    #: Run the roles through the reformulation pass: reword each mission in the
    #: tender's vocabulary and reorder by relevance. Off by default, and that
    #: is deliberate — the deterministic document is the one that can never be
    #: wrong, so a model touching a contractual file is an explicit request.
    adapt: bool = False
    #: What to produce. A CV is one document per retained profile; a
    #: compliance matrix is one document for the whole dossier. The route
    #: branches on this rather than treating the two shapes as one.
    kind: TemplateKind = TemplateKind.CV


@router.post(
    "/shortlists/{shortlist_id}/documents",
    status_code=status.HTTP_201_CREATED,
    summary="Produce one document per retained profile",
)
async def generate_documents(
    shortlist_id: uuid_module.UUID,
    payload: GenerateRequest | None = None,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    request = payload or GenerateRequest()

    shortlist = (
        await session.execute(
            Shortlist.owned_by(principal.tenant).where(Shortlist.id == shortlist_id)
        )
    ).scalar_one_or_none()
    if shortlist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Sélection introuvable.")

    if shortlist.status != ShortlistStatus.VALIDATED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "Cette sélection n'est pas validée. Un document ne peut être produit "
                "qu'à partir d'une sélection verrouillée par un humain."
            ),
        )

    retained = [
        entry
        for entry in shortlist.entries
        if entry.decision == ShortlistDecision.RETAINED.value
    ]
    if not retained:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Aucun profil retenu dans cette sélection. « Personne ne convient » "
                "est une conclusion valable, mais elle ne produit pas de dossier."
            ),
        )

    template = await _template(
        session, principal.tenant, request.template_id, request.kind
    )
    tender = (
        await session.get(Tender, shortlist.tender_id) if shortlist.tender_id else None
    )

    from app.services.storage import get_storage

    storage = get_storage()
    template_bytes = await anyio.to_thread.run_sync(
        lambda: storage.get_bytes(template.storage_key)
    )

    # The reformulation runs on `ai`, never here: it needs the multilingual
    # encoder to check that a rewording still says what the source said, and a
    # 470 MB model has no business in a request process. Same pattern the
    # matching endpoint follows — ask, wait, keep the deterministic result if
    # the answer does not come.
    adapter = _worker_adapter(principal.tenant) if request.adapt else None

    produced: list[dict[str, Any]] = []
    refused: list[dict[str, str]] = []

    if request.kind is TemplateKind.COVER_LETTER:
        produced.append(
            await _produce_letter(
                session,
                storage=storage,
                template_bytes=template_bytes,
                template=template,
                shortlist=shortlist,
                retained=retained,
                tender=tender,
                principal=principal,
            )
        )
        await session.flush()
        logger.info(
            "api.documents_generated",
            shortlist_uuid=str(shortlist_id),
            kind=request.kind.value,
            produced=1,
            actor=principal.identity,
        )
        return {
            "produced": produced,
            "refused": refused,
            "template": f"{template.label} (v{template.version})",
        }

    if request.kind is TemplateKind.COMPLIANCE_MATRIX:
        produced.append(
            await _produce_matrix(
                session,
                storage=storage,
                template_bytes=template_bytes,
                template=template,
                shortlist=shortlist,
                retained=retained,
                tender=tender,
                principal=principal,
            )
        )
        await session.flush()
        logger.info(
            "api.documents_generated",
            shortlist_uuid=str(shortlist_id),
            kind=request.kind.value,
            produced=1,
            actor=principal.identity,
        )
        return {
            "produced": produced,
            "refused": refused,
            "template": f"{template.label} (v{template.version})",
        }

    for entry in retained:
        cv = await session.get(CV, entry.cv_id) if entry.cv_id else None
        if cv is None:
            # A CV deleted at a candidate's request, after the selection was
            # validated. Reported rather than skipped in silence: a dossier
            # short of one expert is something a bid manager must be told.
            refused.append(
                {"label": entry.label, "reason": "Le CV source n'est plus disponible."}
            )
            continue

        try:
            document = await anyio.to_thread.run_sync(
                lambda cv=cv, entry=entry: generate_cv(
                    template_bytes=template_bytes,
                    cv=cv,
                    entry=entry,
                    shortlist=shortlist,
                    tender=tender,
                    adapter=adapter,
                    kind=request.kind.value,
                )
            )
        except GenerationRefused as exc:
            refused.append({"label": entry.label, "reason": str(exc)})
            continue

        version = await _next_version(session, principal.tenant, entry.id)
        document_id = uuid_module.uuid4()
        stored = await anyio.to_thread.run_sync(
            # Both loop variables bound as defaults. The lambda is awaited
            # immediately so late binding would not bite today, but a future
            # edit that gathers these calls would produce every file under the
            # last id — a bug that shows up as documents silently overwriting
            # one another in object storage.
            lambda doc=document, doc_id=document_id: storage.put_bytes(
                storage.build_key(str(doc_id), doc.filename, prefix="documents"),
                doc.content,
                content_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                metadata={"document-id": str(doc_id)},
            )
        )

        row = GeneratedDocument(
            id=document_id,
            tenant_id=principal.tenant,
            shortlist_id=shortlist.id,
            entry_id=entry.id,
            cv_id=entry.cv_id,
            tender_id=shortlist.tender_id,
            template_id=template.id,
            template_key=template.key,
            template_version=template.version,
            kind=request.kind.value,
            label=entry.label,
            filename=document.filename,
            storage_bucket=stored.bucket,
            storage_key=stored.key,
            size_bytes=stored.size_bytes,
            version=version,
            status=GeneratedDocumentStatus.DRAFT.value,
            watermarked=True,
            empty_fields=document.empty_fields,
            adaptation=document.adaptation,
            qa=document.qa,
            generated_by=principal.identity,
        )
        session.add(row)
        produced.append(_to_dict(row))

    await session.flush()
    logger.info(
        "api.documents_generated",
        shortlist_uuid=str(shortlist_id),
        template=f"{template.key} v{template.version}",
        adapt=request.adapt,
        produced=len(produced),
        refused=len(refused),
        actor=principal.identity,
    )
    return {
        "produced": produced,
        # Never empty-and-silent: a profile that did not yield a document says
        # so, with the reason.
        "refused": refused,
        "template": f"{template.label} (v{template.version})",
    }


@router.get("/shortlists/{shortlist_id}/documents", summary="Documents of one selection")
async def list_for_shortlist(
    shortlist_id: uuid_module.UUID,
    current_only: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    query = GeneratedDocument.owned_by(principal.tenant).where(
        GeneratedDocument.shortlist_id == shortlist_id
    )
    if current_only:
        query = query.where(
            GeneratedDocument.status != GeneratedDocumentStatus.SUPERSEDED.value
        )

    rows = (
        (
            await session.execute(
                query.order_by(
                    GeneratedDocument.label, GeneratedDocument.version.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    return {"total": len(rows), "items": [_to_dict(row) for row in rows]}


@router.get("/documents/{document_id}/download", summary="Signed link to a document")
async def download(
    document_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    row = (
        await session.execute(
            GeneratedDocument.owned_by(principal.tenant).where(
                GeneratedDocument.id == document_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document introuvable.")

    from app.services.storage import get_storage

    storage = get_storage()
    url = await anyio.to_thread.run_sync(lambda: storage.presigned_url(row.storage_key))
    return {"url": url, "filename": row.filename}


# ---------------------------------------------------------------------------
async def _produce_matrix(
    session: AsyncSession,
    *,
    storage: Any,
    template_bytes: bytes,
    template: DocumentTemplate,
    shortlist: Shortlist,
    retained: list[Any],
    tender: Any,
    principal: Principal,
) -> dict[str, Any]:
    """One matrix for the whole dossier.

    No model is consulted and none should be: every cell comes from the frozen
    capture — the requirements read from the tender, the technologies it names,
    and the evidence each retained profile was ranked on. A document whose job
    is to say what is *missing* must not be written by something that can
    smooth a gap into a sentence.
    """
    from app.core.identity import utc_now
    from app.services.compliance import build_matrix
    from app.services.generation import WATERMARK

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
        "filigrane": WATERMARK,
    }

    from app.services.templates import render_template

    content = await anyio.to_thread.run_sync(
        lambda: render_template(template_bytes, context)
    )

    label = f"Matrice de conformité — {context['mission_reference'] or 'dossier'}"
    document_id = uuid_module.uuid4()
    filename = f"matrice_conformite_{_slug(context['mission_reference']) or 'dossier'}.docx"
    stored = await anyio.to_thread.run_sync(
        lambda: storage.put_bytes(
            storage.build_key(str(document_id), filename, prefix="documents"),
            content,
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            metadata={"document-id": str(document_id)},
        )
    )

    version = await _next_kind_version(
        session, principal.tenant, shortlist.id, TemplateKind.COMPLIANCE_MATRIX.value
    )
    row = GeneratedDocument(
        id=document_id,
        tenant_id=principal.tenant,
        shortlist_id=shortlist.id,
        tender_id=shortlist.tender_id,
        template_id=template.id,
        template_key=template.key,
        template_version=template.version,
        kind=TemplateKind.COMPLIANCE_MATRIX.value,
        label=label,
        filename=filename,
        storage_bucket=stored.bucket,
        storage_key=stored.key,
        size_bytes=stored.size_bytes,
        version=version,
        status=GeneratedDocumentStatus.DRAFT.value,
        watermarked=True,
        # The gaps are the point of this document, so they are surfaced the
        # same way a CV surfaces its unfilled sections.
        empty_fields=[
            f"{matrix.non_couvertes} exigence(s) non couverte(s)",
            f"{matrix.a_traiter} à rédiger",
        ],
        adaptation={"status": "off", "llm_used": False},
        generated_by=principal.identity,
    )
    session.add(row)
    return _to_dict(row)


async def _produce_letter(
    session: AsyncSession,
    *,
    storage: Any,
    template_bytes: bytes,
    template: DocumentTemplate,
    shortlist: Shortlist,
    retained: list[Any],
    tender: Any,
    principal: Principal,
) -> dict[str, Any]:
    """One covering letter for the whole dossier.

    The brief is built from the compliance matrix: the letter may name the
    technologies the retained team can evidence, and nothing else. The gaps
    are counted and withheld — a model told "do not claim RGAA" writes about
    RGAA.
    """
    from app.core.identity import utc_now
    from app.services.compliance import build_matrix
    from app.services.cover_letter import LetterBrief, compose_letter
    from app.services.generation import WATERMARK
    from app.services.memory import recall_responses
    from app.services.templates import render_template

    matrix = build_matrix(shortlist, retained)
    covered = [row.label for row in matrix.rows if row.statut == "couverte"]

    reference = getattr(tender, "reference", None) or ""
    title = getattr(tender, "title", None) or shortlist.tender_title or ""
    deadline = (
        tender.deadline.strftime("%d/%m/%Y")
        if tender is not None and getattr(tender, "deadline", None)
        else ""
    )

    brief = LetterBrief(
        buyer=getattr(tender, "buyer", None) or "",
        reference=reference,
        title=title,
        deadline=deadline,
        country=getattr(tender, "country", None) or "",
        team=matrix.profils,
        covered=covered,
        gaps=matrix.non_couvertes,
        examples=[
            item.text
            for item in await anyio.to_thread.run_sync(
                lambda: recall_responses(title, tenant=principal.tenant, limit=2)
            )
        ],
    )

    letter = await anyio.to_thread.run_sync(lambda: compose_letter(brief))

    context = {
        "paragraphes": letter.paragraphs,
        "equipe": matrix.profils,
        "competences": covered,
        "mission_titre": title,
        "mission_acheteur": brief.buyer,
        "mission_reference": reference,
        "mission_pays": brief.country,
        "mission_echeance": deadline,
        "genere_le": utc_now().strftime("%d/%m/%Y"),
        "filigrane": WATERMARK,
    }
    content = await anyio.to_thread.run_sync(
        lambda: render_template(template_bytes, context)
    )

    document_id = uuid_module.uuid4()
    filename = f"lettre_{_slug(reference) or 'dossier'}.docx"
    stored = await anyio.to_thread.run_sync(
        lambda: storage.put_bytes(
            storage.build_key(str(document_id), filename, prefix="documents"),
            content,
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            metadata={"document-id": str(document_id)},
        )
    )

    version = await _next_kind_version(
        session, principal.tenant, shortlist.id, TemplateKind.COVER_LETTER.value
    )
    row = GeneratedDocument(
        id=document_id,
        tenant_id=principal.tenant,
        shortlist_id=shortlist.id,
        tender_id=shortlist.tender_id,
        template_id=template.id,
        template_key=template.key,
        template_version=template.version,
        kind=TemplateKind.COVER_LETTER.value,
        label=f"Lettre d'accompagnement — {reference or 'dossier'}",
        filename=filename,
        storage_bucket=stored.bucket,
        storage_key=stored.key,
        size_bytes=stored.size_bytes,
        version=version,
        status=GeneratedDocumentStatus.DRAFT.value,
        watermarked=True,
        empty_fields=(
            [f"{matrix.non_couvertes} exigence(s) non couverte(s), non mentionnées"]
            if matrix.non_couvertes
            else []
        ),
        adaptation=letter.to_dict(),
        generated_by=principal.identity,
    )
    session.add(row)
    return _to_dict(row)


async def _next_kind_version(
    session: AsyncSession, tenant: str, shortlist_id: uuid_module.UUID, kind: str
) -> int:
    """Supersede this dossier's previous document of one kind, and number after it.

    Per kind rather than per dossier: regenerating the letter must not mark
    the matrix stale, and vice versa.
    """
    previous = (
        (
            await session.execute(
                GeneratedDocument.owned_by(tenant)
                .where(GeneratedDocument.shortlist_id == shortlist_id)
                .where(GeneratedDocument.kind == kind)
                .order_by(GeneratedDocument.version.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in previous:
        row.status = GeneratedDocumentStatus.SUPERSEDED.value
    return (previous[0].version + 1) if previous else 1


def _slug(value: str) -> str:
    import unicodedata

    ascii_only = (
        unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    )
    cleaned = "".join(c if c.isalnum() else "_" for c in ascii_only)
    return "_".join(part for part in cleaned.split("_") if part)[:60]


def _worker_adapter(tenant: str) -> Any:
    """Hand `generate_cv` a way to reach the model-bound worker."""
    from app.workers.celery_app import TASK_PRIORITY
    from app.workers.tasks.generation import adapt_cv_experiences

    def _adapt(
        experiences: list[dict[str, Any]], requirements: list[str], limit: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        task = adapt_cv_experiences.apply_async(
            kwargs={
                "experiences": experiences,
                "requirements": requirements,
                "limit": limit,
                "tenant": tenant,
            },
            queue="ai",
            priority=TASK_PRIORITY["interactive"],
        )
        outcome = task.get(timeout=_ADAPT_TIMEOUT_SECONDS, propagate=True)
        return outcome.get("experiences") or [], outcome.get("report") or {}

    return _adapt


async def _template(
    session: AsyncSession,
    tenant: str,
    template_id: uuid_module.UUID | None,
    kind: TemplateKind = TemplateKind.CV,
) -> DocumentTemplate:
    query = DocumentTemplate.owned_by(tenant).where(DocumentTemplate.kind == kind.value)
    query = (
        query.where(DocumentTemplate.id == template_id)
        if template_id is not None
        else query.where(DocumentTemplate.is_active.is_(True))
    )
    row = (await session.execute(query.limit(1))).scalar_one_or_none()
    if row is None:
        script = (
            "scripts/build_matrix_template.py"
            if kind is TemplateKind.COMPLIANCE_MATRIX
            else "scripts/build_default_template.py"
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Aucun gabarit actif pour « {kind.value} ». Déposez-en un dans "
                f"Administration → Gabarits, ou construisez celui par défaut "
                f"avec `python {script}`."
            ),
        )
    return row


async def _next_version(
    session: AsyncSession, tenant: str, entry_id: uuid_module.UUID
) -> int:
    """Supersede what came before, and number this one after it."""
    previous = (
        (
            await session.execute(
                GeneratedDocument.owned_by(tenant)
                .where(GeneratedDocument.entry_id == entry_id)
                .order_by(GeneratedDocument.version.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in previous:
        # Kept, not deleted: a version already circulated has to remain
        # explainable, and a diff between two versions is what the review step
        # will need.
        row.status = GeneratedDocumentStatus.SUPERSEDED.value
    return (previous[0].version + 1) if previous else 1


def _to_dict(row: GeneratedDocument) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "label": row.label,
        "filename": row.filename,
        "kind": row.kind,
        "version": row.version,
        "status": row.status,
        "watermarked": row.watermarked,
        "size_bytes": row.size_bytes,
        "template": (
            f"{row.template_key} v{row.template_version}" if row.template_key else None
        ),
        # Shown in the interface: the platform states what it could not fill
        # rather than letting a reviewer find a blank section in a submitted
        # dossier.
        "empty_fields": list(row.empty_fields or []),
        # Whether a model touched the wording, and what the anti-invention
        # guard caught. Shown in the interface: a reviewer is entitled to know
        # this about the document they are about to sign.
        "adaptation": dict(row.adaptation or {}),
        # What the automatic review found. A document that passed and a
        # document nobody checked are different things, and only one of them
        # should reach a reviewer unannounced.
        "qa": dict(row.qa or {}),
        "generated_by": row.generated_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


__all__ = ["router"]
