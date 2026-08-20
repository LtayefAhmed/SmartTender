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
from collections.abc import Callable
from typing import Any

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


def _after_commit(session: AsyncSession, action: Callable[[], None]) -> None:
    """Run ``action`` once the transaction is durably committed.

    Publishing a task inside the transaction is a classic and genuinely nasty
    bug: Redis is faster than PostgreSQL's commit, so the worker starts,
    queries for the row, finds nothing, and fails — intermittently, and only
    under load. The ingestion path guards against this the same way.

    The listener goes on ``sync_session``: SQLAlchemy's ORM events fire on the
    synchronous session that an ``AsyncSession`` drives, not on the wrapper.
    """
    from sqlalchemy import event

    def _run(_session: object) -> None:
        try:
            action()
        except Exception as exc:
            # The row is committed and the backlog task will pick it up; a
            # broker hiccup must not make an accepted import look failed.
            logger.error(
                "api.cv_dispatch_failed", error=str(exc)[:200], error_type=type(exc).__name__
            )

    # ``once=True`` rather than self-removal, which mutates the collection
    # SQLAlchemy is iterating.
    event.listen(session.sync_session, "after_commit", _run, once=True)


def _clean_folder(value: str | None) -> str | None:
    """Normalise a browser-supplied relative path into a folder.

    ``webkitRelativePath`` gives ``CVs/SAP/dupont.pdf``; what is worth keeping
    is ``CVs/SAP``. Backslashes are folded to forward slashes because the same
    tree imported from Windows and from macOS must land in one folder, not two.
    Traversal segments are dropped — this value is stored and displayed, never
    used to build a path, but a stored ``../`` is a trap for whoever does.
    """
    if not value:
        return None
    parts = [
        segment.strip()
        for segment in value.replace("\\", "/").split("/")
        if segment.strip() and segment.strip() not in {".", ".."}
    ]
    return "/".join(parts)[:512] or None


@router.get("", response_model=Page[CVRead], summary="List imported CVs")
async def list_cvs(
    session: AsyncSession = Depends(get_session),
    params: PaginationParams = Depends(pagination),
    principal: Principal = Depends(require_principal),
) -> Page[CVRead]:
    total = (
        await session.execute(
            select(func.count(CV.id)).where(CV.tenant_id == principal.tenant)
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                CV.owned_by(principal.tenant)
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
    folder: str | None = Form(default=None),
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

    # Deduplicated on the bytes, before anything is stored — otherwise a
    # re-import leaves an orphan object behind even when the row is refused.
    #
    # The usage this protects: a firm imports a folder of several hundred CVs,
    # then imports it again a month later. Without this, every candidate exists
    # twice and a shortlist shows the same person on two lines. Identical bytes
    # are the same document; a genuinely updated CV hashes differently and is
    # correctly a new row.
    fingerprint = sha256_bytes(validated.content)
    existing = (
        await session.execute(
            CV.owned_by(principal.tenant).where(CV.sha256 == fingerprint).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        # A duplicate still teaches us something when it arrives with a folder
        # the original lacked. Re-importing a tree is how anyone would expect
        # to backfill that, and refusing outright would make the arrangement
        # unrecoverable for CVs already stored.
        if folder and not existing.folder:
            existing.folder = _clean_folder(folder)
        logger.info(
            "api.cv_duplicate_ignored",
            cv_id=str(existing.id),
            filename=validated.filename,
            folder_backfilled=bool(folder and existing.folder),
            actor=principal.identity,
        )
        return CVRead.model_validate(existing).model_copy(update={"is_duplicate": True})

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
        tenant_id=principal.tenant,
        original_filename=validated.filename,
        storage_bucket=stored.bucket,
        storage_key=stored.key,
        content_type=validated.content_type,
        size_bytes=stored.size_bytes,
        sha256=fingerprint,
        folder=_clean_folder(folder),
        source=source if source in ("upload", "link") else "upload",
        source_url=source_url,
        uploaded_by=principal.identity,
    )
    session.add(row)
    await session.flush()

    # Published after the transaction commits, not inside it: a worker is fast
    # enough to pick the job up and query for a row the database has not yet
    # made visible. The same rule the ingestion path follows.
    cv_key = str(cv_id)

    def _dispatch_extraction() -> None:
        from app.workers.tasks.cvs import extract_cv_text

        extract_cv_text.apply_async(kwargs={"cv_id": cv_key}, queue="ocr")

    _after_commit(session, _dispatch_extraction)

    logger.info(
        "api.cv_imported",
        cv_id=str(cv_id),
        filename=validated.filename,
        size_bytes=validated.size_bytes,
        source=row.source,
        actor=principal.identity,
    )
    return CVRead.model_validate(row)


@router.get("/{cv_id}/download", summary="Get a time-limited link to the original CV")
async def download_cv(
    cv_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Return a presigned URL for the stored file.

    Import without consultation is half a feature: a recruiter comparing two
    profiles has to be able to open them, and the object store is not an
    interface. The same gap existed on tender attachments and had the same
    consequence — the only way to read a stored file was to browse MinIO by
    hand.

    The API never proxies the bytes. Streaming a CV through the request handler
    would hold a worker for the whole transfer; instead the browser is handed a
    URL and fetches the object store directly.
    """
    # Fetched through the tenant filter, not by primary key: a UUID from
    # another organisation must read as "not found", never as "forbidden" —
    # the second answer confirms the row exists.
    row = (
        await session.execute(CV.owned_by(principal.tenant).where(CV.id == cv_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CV not found.")
    if not row.storage_key:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "message": "This CV has a record but no stored file.",
                "original_filename": row.original_filename,
            },
        )

    from app.services.storage import get_storage

    storage = get_storage()
    url = await anyio.to_thread.run_sync(lambda: storage.presigned_url(row.storage_key))
    logger.info("api.cv_link_issued", cv_id=str(cv_id), actor=principal.identity)
    return {
        "url": url,
        "expires_in_seconds": storage.presigned_ttl,
        "filename": row.original_filename,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
    }


@router.delete("/{cv_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove an imported CV")
async def delete_cv(
    cv_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> None:
    row = (
        await session.execute(CV.owned_by(principal.tenant).where(CV.id == cv_id))
    ).scalar_one_or_none()
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
