"""Entry point B — manual document upload.

Validation is synchronous and everything after it is not. The user gets an
immediate, specific answer about their file ("this PDF contains embedded
JavaScript") rather than an opaque failure ten minutes later in a worker log —
and the server does no parsing, no OCR and no scoring inside the request.

A rejected file is never stored, never queued, and never given a UUID. That is
enforced by ordering: validation runs before anything is written anywhere.
"""

from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.connectors.models import NormalizedTender
from app.core.enums import EntryPoint
from app.core.exceptions import ValidationError
from app.core.identity import canonicalize_url
from app.core.logging import get_logger
from app.core.metrics import tenders_rejected_total
from app.schemas.common import AcceptedResponse
from app.services.validation import UploadValidator

logger = get_logger(__name__)
router = APIRouter(prefix="/upload", tags=["upload"])

#: Connector key recorded for documents that did not come from a portal.
MANUAL_SOURCE_KEY = "manual"


@router.post(
    "",
    response_model=AcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a tender document",
)
async def upload_document(
    file: UploadFile = File(..., description="PDF, DOCX or HTML, up to 25 MB."),
    title: str | None = Form(default=None),
    buyer: str | None = Form(default=None),
    country: str | None = Form(default=None),
    source_url: str | None = Form(default=None),
    reference: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> AcceptedResponse:
    """Validate a document and admit it to the pipeline."""
    validator = UploadValidator()

    # Read through the validator so the size limit is enforced while reading,
    # not after a large buffer already exists.
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

    resolved_title = (title or "").strip() or _title_from(validated)
    if not resolved_title:
        tenders_rejected_total.labels(entry_point="manual_upload", reason="no_title").inc()
        raise ValidationError(
            "No title could be determined. Provide one in the `title` field.",
            field="title",
        )

    tender = NormalizedTender(
        connector_key=MANUAL_SOURCE_KEY,
        source_url=source_url,
        canonical_url=canonicalize_url(source_url) if source_url else None,
        reference=(reference or "").strip() or None,
        title=resolved_title,
        description=validated.text_preview,
        buyer=(buyer or "").strip() or None,
        country=(country or "").strip() or None,
        raw_sha256=validated.fingerprint.get("raw_sha256"),
        text_sha256=validated.fingerprint.get("text_sha256"),
        extra={"uploaded_by": principal.identity, "original_filename": validated.original_filename},
    )

    from app.services.ingestion import IngestionService

    def _ingest(sync_session) -> dict:  # type: ignore[no-untyped-def]
        return IngestionService().ingest_tender(
            sync_session,
            tender,
            entry_point=EntryPoint.MANUAL_UPLOAD,
            raw_content=validated.content,
            content_type=validated.content_type,
            original_filename=validated.filename,
            enqueue=_enqueue_processing,
            actor=principal.identity,
        ).to_dict()

    # The ingestion service is synchronous because it is shared with the Celery
    # workers. ``run_sync`` lends it the request's own session and transaction
    # rather than opening a second connection behind the API's back — the
    # upload and its audit trail therefore commit together, or not at all.
    outcome = await session.run_sync(_ingest)

    if not outcome["accepted"]:
        if outcome["reason"] == "duplicate":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "duplicate_tender",
                    "message": "This document is already known to the platform.",
                    "canonical_tender_id": outcome["duplicate_of"],
                    "matched_by": outcome["duplicate_strategy"],
                },
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "not_ingested", "message": outcome["reason"]},
        )

    logger.info(
        "api.upload_accepted",
        tender_uuid=outcome["tender_id"],
        filename=validated.filename,
        size_bytes=validated.size_bytes,
        actor=principal.identity,
    )
    message = f"'{validated.filename}' accepted and queued for processing."
    if validated.warnings:
        message += " " + " ".join(validated.warnings)

    return AcceptedResponse(
        message=message,
        tender_id=outcome["tender_id"],
        poll_url=f"/tenders/{outcome['tender_id']}",
    )


def _title_from(validated) -> str | None:  # type: ignore[no-untyped-def]
    """Best-effort title: the first substantial line of extracted text."""
    if validated.text_preview:
        for line in validated.text_preview.splitlines():
            candidate = line.strip()
            if 8 <= len(candidate) <= 300:
                return candidate
    stem = validated.filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
    return stem or None


def _enqueue_processing(tender_id) -> None:  # type: ignore[no-untyped-def]
    from app.workers.tasks.pipeline import process_tender

    process_tender.apply_async(kwargs={"tender_id": str(tender_id)}, queue="parsing")
