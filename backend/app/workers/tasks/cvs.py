"""Turning a stored CV into readable text.

The import stores bytes and writes a row. Nothing read them, which meant a CV
was a file rather than a profile: matching compares a tender's requirements
against a candidate's skills, and those skills only exist once the PDF has
become text.

The extractor is the one already built for tender attachments — digital text
first, per-page OCR only where the text layer came back empty. CVs exercise it
differently: a cahier des charges is prose, while an Inetum CV puts its
technologies in two-column tables (``OUTILS :``, ``LANGAGES / AGL :``). If those
come back as readable lines, the extractor holds for both shapes.

Routed to ``ocr`` because a scanned CV is exactly the case that needs it, and
that queue is the one sized for it.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger, log_context
from app.db.models.cv import CV
from app.db.session import session_scope
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = ["extract_cv_text", "extract_pending_cvs"]


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.cvs.extract_cv_text",
    queue="ocr",
    max_retries=1,
)
def extract_cv_text(self: PipelineTask, cv_id: str) -> dict[str, Any]:
    """Read one CV into text.

    Never raises on a document it cannot read. A CV that fails extraction stays
    imported, listed and downloadable — it simply cannot be matched, and the
    row says so instead of pretending otherwise.
    """
    from app.services.cv_criteria import extract_criteria
    from app.services.extraction import clean_extracted_text, get_extractor
    from app.services.profiles import extract_identity
    from app.services.storage import get_storage

    with log_context(cv_id=cv_id):
        with session_scope() as session:
            row = session.get(CV, uuid_module.UUID(cv_id))
            if row is None:
                logger.warning("cv.extraction_row_missing")
                return {"cv_id": cv_id, "status": "missing"}
            storage_key = row.storage_key
            content_type = row.content_type
            filename = row.original_filename

        outcome: dict[str, Any] = {"cv_id": cv_id}
        try:
            content = get_storage().get_bytes(storage_key)
            result = get_extractor().extract(
                content, content_type=content_type, filename=filename
            )
            text, truncated = clean_extracted_text(result.text)
        except Exception as exc:
            logger.warning("cv.extraction_failed", error=str(exc)[:200])
            with session_scope() as session:
                row = session.get(CV, uuid_module.UUID(cv_id))
                if row is not None:
                    row.extraction_status = "failed"
                    row.extraction_error = f"{type(exc).__name__}: {exc}"[:2000]
            return {"cv_id": cv_id, "status": "failed", "error": str(exc)[:200]}

        with session_scope() as session:
            row = session.get(CV, uuid_module.UUID(cv_id))
            if row is None:
                return {"cv_id": cv_id, "status": "missing"}

            row.extracted_text = text or None
            row.extraction_chars = len(text)
            row.extraction_method = result.method
            row.extraction_error = result.error
            # "empty" rather than "failed" when a readable file yields nothing:
            # an image-only CV with no OCR available is a different problem from
            # a corrupt file, and the fix is different too.
            row.extraction_status = "extracted" if text else "empty"

            # Resolved once here rather than in the interface, so the modal, an
            # export and a future notification all say the same thing.
            identity = extract_identity(text, filename=filename)
            row.display_name = identity.name
            row.headline = identity.headline
            row.identity_source = identity.source

            # Read here rather than at search time: a recruiter filtering over
            # several hundred profiles cannot wait for several hundred
            # documents to be parsed, and the answer does not change between
            # two searches.
            row.criteria = extract_criteria(text).as_dict()
            outcome.update(
                identity=identity.source,
                label=identity.label[:60],
                status=row.extraction_status,
                chars=row.extraction_chars,
                method=result.method,
                truncated=truncated,
            )

        logger.info("cv.extracted", **{k: v for k, v in outcome.items() if k != "cv_id"})

        # Text is what a human reads; vectors are what a tender is matched
        # against. Dispatched rather than called so a slow model never holds
        # the OCR queue, and so a vector store outage cannot fail extraction.
        if outcome.get("status") == "extracted":
            from app.workers.tasks.indexing import index_cv

            index_cv.apply_async(kwargs={"cv_id": cv_id}, queue="ai")
        return outcome


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.cvs.extract_pending_cvs",
    queue="ocr",
    max_retries=0,
)
def extract_pending_cvs(self: PipelineTask, limit: int = 100) -> dict[str, Any]:
    """Read every CV imported before extraction existed.

    Lowering a gate fixes new arrivals and does nothing for what is already
    stored — the same lesson the enrichment backlog taught. Idempotent: a CV
    already extracted is not selected, so this is safe to re-run.
    """
    with session_scope() as session:
        pending = [
            str(row[0])
            for row in session.execute(
                select(CV.id)
                .where(CV.extraction_status.in_(["pending", "failed"]))
                .order_by(CV.created_at)
                .limit(limit)
            ).all()
        ]

    for cv_id in pending:
        extract_cv_text.apply_async(kwargs={"cv_id": cv_id}, queue="ocr")

    logger.info("cv.backlog_dispatched", count=len(pending))
    return {"dispatched": len(pending)}
