"""The document template library.

Uploading is the whole feature. Everything else here exists to make an upload
safe and to make a bad one fail early:

* the file goes through the same validator as any other upload, so a template
  carrying a VBA project is refused for exactly the reason a macro-bearing CV
  is — a .docx with a ``vbaProject.bin`` is not a document, it is a payload;
* the placeholders are read out of the file and checked against what the render
  context can supply, and an unknown name is a rejection with the list of names
  attached, not a silent acceptance that fails weeks later in front of a
  deadline;
* re-uploading the same key makes a new version rather than overwriting one, so
  a document generated last month can still be explained.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.core.enums import TemplateKind
from app.core.identity import sha256_bytes
from app.core.logging import get_logger
from app.db.models.template import DocumentTemplate
from app.services.templates import (
    TemplateError,
    check_variables,
    discover_variables,
    variables_for,
)
from app.services.validation import UploadValidator

logger = get_logger(__name__)
router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("/variables", summary="What a template of a given kind may ask for")
async def catalogue(
    kind: TemplateKind = Query(default=TemplateKind.CV),
    _: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """The catalogue, for whoever is writing the Word file.

    Served rather than documented in a wiki because it is the one piece of
    information a template author cannot guess, and the one that goes stale
    first when it lives anywhere but the code that enforces it.
    """
    return {
        "kind": kind.value,
        "variables": [
            {
                "name": variable.name,
                "kind": variable.kind,
                "label": variable.label,
                "example": variable.example,
                "available": variable.available,
            }
            for variable in variables_for(kind.value)
        ],
    }


@router.get("", summary="List templates")
async def list_templates(
    kind: TemplateKind | None = Query(default=None),
    active_only: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    query = DocumentTemplate.owned_by(principal.tenant)
    if kind is not None:
        query = query.where(DocumentTemplate.kind == kind.value)
    if active_only:
        query = query.where(DocumentTemplate.is_active.is_(True))

    rows = (
        (
            await session.execute(
                query.order_by(
                    DocumentTemplate.kind, DocumentTemplate.key, DocumentTemplate.version.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    return {"total": len(rows), "items": [_to_dict(row) for row in rows]}


@router.post("", status_code=status.HTTP_201_CREATED, summary="Upload a template")
async def upload_template(
    file: UploadFile = File(..., description="A .docx carrying Jinja placeholders."),
    key: str = Form(..., max_length=128),
    label: str = Form(..., max_length=255),
    kind: TemplateKind = Form(default=TemplateKind.CV),
    funder: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Accept a template, or explain precisely why not."""
    validator = UploadValidator()
    content = await anyio.to_thread.run_sync(
        lambda: validator.read_stream(file.file, declared_size=file.size)
    )
    validated = await anyio.to_thread.run_sync(
        lambda: validator.validate(
            content, filename=file.filename, declared_content_type=file.content_type
        )
    )
    if not validated.filename.lower().endswith(".docx"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Un gabarit doit être un fichier .docx.",
        )

    # Parsing happens off the event loop: reading a document's XML and walking
    # its Jinja tree is CPU work, and a large template would otherwise stall
    # every other request on this worker.
    try:
        found = await anyio.to_thread.run_sync(lambda: discover_variables(validated.content))
    except TemplateError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    unknown, unavailable = check_variables(found, kind=kind.value)
    if unknown:
        # Refused at the door, with the names. The person is still holding the
        # file; telling them "some variable is wrong" at generation time, three
        # weeks later, is how a tool gets abandoned.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Ce gabarit demande des variables que la plateforme ne sait pas "
                f"fournir : {', '.join(unknown)}. "
                "Consultez /templates/variables pour la liste des noms acceptés."
            ),
        )

    slug = _clean_key(key)
    version = (
        await session.execute(
            select(func.coalesce(func.max(DocumentTemplate.version), 0)).where(
                DocumentTemplate.tenant_id == principal.tenant,
                DocumentTemplate.key == slug,
            )
        )
    ).scalar_one() + 1

    from app.services.storage import get_storage

    template_id = uuid_module.uuid4()
    storage = get_storage()
    stored = await anyio.to_thread.run_sync(
        lambda: storage.put_bytes(
            storage.build_key(str(template_id), validated.filename, prefix="templates"),
            validated.content,
            content_type=validated.content_type,
            metadata={"template-id": str(template_id), "uploaded-by": principal.identity},
        )
    )

    # Only one active version per key. The previous one stays readable — a
    # document generated from it must remain explainable — it simply stops
    # being what a new generation picks up.
    await session.execute(
        DocumentTemplate.__table__.update()
        .where(
            DocumentTemplate.tenant_id == principal.tenant,
            DocumentTemplate.key == slug,
        )
        .values(is_active=False)
    )

    row = DocumentTemplate(
        id=template_id,
        tenant_id=principal.tenant,
        key=slug,
        label=label,
        kind=kind.value,
        funder=(funder or None),
        version=version,
        is_active=True,
        original_filename=validated.filename,
        storage_bucket=stored.bucket,
        storage_key=stored.key,
        content_type=validated.content_type,
        size_bytes=stored.size_bytes,
        sha256=sha256_bytes(validated.content),
        variables=sorted(found),
        uploaded_by=principal.identity,
        notes=notes,
    )
    session.add(row)
    await session.flush()

    logger.info(
        "api.template_uploaded",
        template_uuid=str(template_id),
        key=slug,
        version=version,
        kind=kind.value,
        variables=len(found),
        unavailable=len(unavailable),
        actor=principal.identity,
    )
    payload = _to_dict(row)
    # Not a rejection: the name is real, the data is not there yet, and the
    # section will render empty until it is. Saying so at upload beats letting
    # someone discover it in a produced document.
    payload["unavailable_variables"] = unavailable
    return payload


@router.get("/{template_id}/download", summary="Signed link to the .docx")
async def download_template(
    template_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    row = await _load(session, template_id, principal.tenant)
    from app.services.storage import get_storage

    storage = get_storage()
    url = await anyio.to_thread.run_sync(lambda: storage.presigned_url(row.storage_key))
    return {"url": url, "filename": row.original_filename}


@router.delete("/{template_id}", summary="Retire a template version")
async def retire_template(
    template_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Deactivate rather than delete.

    The row and the file stay. A document generated from this version has to
    remain explainable, and "the template was deleted" is not an explanation.
    """
    row = await _load(session, template_id, principal.tenant)
    row.is_active = False
    await session.flush()
    logger.info(
        "api.template_retired", template_uuid=str(template_id), actor=principal.identity
    )
    return _to_dict(row)


def _to_dict(row: DocumentTemplate) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "key": row.key,
        "label": row.label,
        "kind": row.kind,
        "funder": row.funder,
        "version": row.version,
        "is_active": row.is_active,
        "original_filename": row.original_filename,
        "size_bytes": row.size_bytes,
        "variables": list(row.variables or []),
        "uploaded_by": row.uploaded_by,
        "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _clean_key(value: str) -> str:
    """A slug, so a key is safe in a URL and stable across re-uploads."""
    cleaned = "".join(c if c.isalnum() else "_" for c in value.strip().lower())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    if not cleaned:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="La clé du gabarit est vide."
        )
    return cleaned[:128]


async def _load(
    session: AsyncSession, template_id: uuid_module.UUID, tenant: str
) -> DocumentTemplate:
    row = (
        await session.execute(
            DocumentTemplate.owned_by(tenant).where(DocumentTemplate.id == template_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Gabarit introuvable.")
    return row


__all__ = ["router"]
