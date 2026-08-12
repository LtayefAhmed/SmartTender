"""CV import — phase one of matching.

Mirrors ``upload.py``: validate synchronously, store bytes, persist the row.
A rejected file is never stored and never gets a row. There is deliberately
no server-side URL fetch here — the browser downloads a "link" import and
hands over the bytes through the exact same path as a picked file, so this
endpoint never has to resolve an operator-supplied address itself. Matching
against tenders is a later phase and not implemented yet; this is only the
import.
"""

from __future__ import annotations

import uuid as uuid_module

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, pagination, require_principal
from app.core.identity import sha256_bytes
from app.core.logging import get_logger
from app.db.models.cv import CV
from app.schemas.common import Page, PaginationParams
from app.schemas.cv import CVRead
from app.services.validation import UploadValidator

logger = get_logger(__name__)
router = APIRouter(prefix="/cvs", tags=["cvs"])


@router.get("", response_model=Page[CVRead], summary="List imported CVs")
async def list_cvs(
    session: AsyncSession = Depends(get_session),
    params: PaginationParams = Depends(pagination),
    _: Principal = Depends(require_principal),
) -> Page[CVRead]:
    total = (await session.execute(select(func.count(CV.id)))).scalar_one()
    rows = (
        (
            await session.execute(
                select(CV)
                .order_by(desc(CV.created_at))
                .offset(params.offset)
                .limit(params.page_size)
            )
        )
        .scalars()
        .all()
    )
    return Page.build([CVRead.model_validate(row) for row in rows], total, params)


@router.post(
    "",
    response_model=CVRead,
    status_code=status.HTTP_201_CREATED,
    summary="Import a CV",
)
async def import_cv(
    file: UploadFile = File(..., description="PDF or DOCX, up to the configured size limit."),
    source: str = Form(default="upload"),
    source_url: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> CVRead:
    """Validate a CV and store it. No parsing, no matching — that is a later phase."""
    validator = UploadValidator()

    content = await anyio.to_thread.run_sync(
        lambda: validator.read_stream(file.file, declared_size=file.size)
    )
    validated = await anyio.to_thread.run_sync(
        lambda: validator.validate(
            content,
            filename=file.filename,
            declared_content_type=file.content_type,
        )
    )

    from app.services.storage import get_storage

    cv_id = uuid_module.uuid4()
    storage = get_storage()
    stored = await anyio.to_thread.run_sync(
        lambda: storage.put_bytes(
            storage.build_key(str(cv_id), validated.filename, prefix="cvs"),
            validated.content,
            content_type=validated.content_type,
            metadata={"cv-id": str(cv_id), "uploaded-by": principal.identity},
        )
    )

    row = CV(
        id=cv_id,
        original_filename=validated.filename,
        storage_bucket=stored.bucket,
        storage_key=stored.key,
        content_type=validated.content_type,
        size_bytes=stored.size_bytes,
        sha256=sha256_bytes(validated.content),
        source=source if source in ("upload", "link") else "upload",
        source_url=source_url,
        uploaded_by=principal.identity,
    )
    session.add(row)
    await session.flush()

    logger.info(
        "api.cv_imported",
        cv_id=str(cv_id),
        filename=validated.filename,
        size_bytes=validated.size_bytes,
        source=row.source,
        actor=principal.identity,
    )
    return CVRead.model_validate(row)


@router.delete("/{cv_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove an imported CV")
async def delete_cv(
    cv_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> None:
    row = await session.get(CV, cv_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CV not found.")

    from app.services.storage import get_storage

    try:
        await anyio.to_thread.run_sync(lambda: get_storage().delete(row.storage_key))
    except Exception as exc:
        # The row is what the UI reads; an orphaned object in MinIO is a
        # cleanup detail, not a reason to leave a deleted-looking CV visible.
        logger.warning("api.cv_storage_delete_failed", cv_id=str(cv_id), error=str(exc)[:200])

    await session.delete(row)
    logger.info("api.cv_deleted", cv_id=str(cv_id), actor=principal.identity)
