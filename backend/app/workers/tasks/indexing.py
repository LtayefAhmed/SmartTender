"""Putting passages into the vector index.

The last link before matching. A tender or a CV whose text was extracted is
readable by a human; only once its passages are embedded and indexed is it
comparable to anything else.

Three decisions worth stating.

**Ids are derived, never random.** A point's id is a UUID5 of the owning row
and the passage position, so re-indexing a document overwrites its passages
instead of adding a second copy. With random ids, every re-extraction would
leave the previous generation searchable forever — and nothing would report it,
because the index would simply hold more points than the source justifies.

**Old passages are deleted before new ones are written.** Overwriting handles a
document that stayed the same length or grew; a document that *shrank* leaves
orphans at the tail. Those orphans still match, still rank, and describe text
that no longer exists.

**Failure is never fatal.** The index is derived from ``extracted_text``: a
tender that fails to index is still collected, scored and notified, and can be
re-indexed at any time. Nothing here is allowed to break the pipeline it hangs
off.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from sqlalchemy import select

from app.core.logging import get_logger, log_context
from app.db.models.cv import CV
from app.db.models.tender import Tender
from app.db.session import session_scope
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = ["index_cv", "index_pending", "index_tender"]

#: Namespace for deterministic point ids. Any fixed UUID works; what matters is
#: that it never changes, or every document silently re-indexes under new ids
#: and the old ones stay behind.
_NAMESPACE = uuid_module.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

#: Tenders are public notices, shared by every organisation on the platform.
#: The field is still written so payloads have one shape and a future
#: tenant-scoped tender filter needs no migration.
_PUBLIC = "public"


def _point_id(owner_id: str, position: int) -> str:
    return str(uuid_module.uuid5(_NAMESPACE, f"{owner_id}:{position}"))


def _index_document(
    *,
    collection: str,
    owner_id: str,
    tenant: str,
    text: str,
    extra_payload: dict[str, Any],
) -> dict[str, Any]:
    """Chunk, encode and index one document. Shared by tenders and CVs."""
    from app.services.chunking import chunk_text
    from app.services.embeddings import get_embedder
    from app.services.vectors import VectorPoint, get_vector_store

    chunks = chunk_text(text)
    store = get_vector_store()

    # Cleared first: overwriting by id handles a document that grew, but one
    # that shrank would keep its tail indexed and matchable forever.
    store.delete_owner(collection, owner_id)
    if not chunks:
        return {"passages": 0, "indexed": 0}

    embedder = get_embedder()
    vectors = embedder.encode_many([chunk.text for chunk in chunks])

    points = [
        VectorPoint(
            id=_point_id(owner_id, chunk.index),
            vector=vector,
            payload={
                "tenant_id": tenant,
                "owner_id": owner_id,
                "document": chunk.document,
                "priority": chunk.priority,
                "position": chunk.index,
                "text": chunk.text,
                **extra_payload,
            },
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]

    written = store.upsert(collection, points, dimensions=embedder.dimensions)
    return {"passages": len(chunks), "indexed": written}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.indexing.index_tender",
    queue="ai",
    max_retries=1,
)
def index_tender(self: PipelineTask, tender_id: str) -> dict[str, Any]:
    """Index a tender's passages so candidates can be matched against it."""
    from app.core.config import get_settings

    with log_context(tender_uuid=tender_id):
        with session_scope() as session:
            row = session.get(Tender, uuid_module.UUID(tender_id))
            if row is None:
                return {"tender_id": tender_id, "status": "missing"}
            # The publication and the dossier both carry requirements: for the
            # 355 notices with no attachment, the publication is all there is.
            parts = [part for part in (row.description, row.extracted_text) if part]
            text = "\n\n".join(parts)
            payload = {
                "title": (row.title or "")[:300],
                "country": row.country,
                "source_key": row.source_key,
            }

        try:
            outcome = _index_document(
                collection=get_settings().vector.tender_collection,
                owner_id=tender_id,
                tenant=_PUBLIC,
                text=text,
                extra_payload=payload,
            )
        except Exception as exc:
            # Derived data: the tender is intact and can be re-indexed. Never
            # let this break the chain it hangs off.
            logger.warning("indexing.tender_failed", error=str(exc)[:200])
            return {"tender_id": tender_id, "status": "failed", "error": str(exc)[:200]}

        logger.info("indexing.tender_indexed", **outcome)
        return {"tender_id": tender_id, **outcome}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.indexing.index_cv",
    queue="ai",
    max_retries=1,
)
def index_cv(self: PipelineTask, cv_id: str) -> dict[str, Any]:
    """Index a CV's passages, under its owning organisation."""
    from app.core.config import get_settings

    with log_context(cv_id=cv_id):
        with session_scope() as session:
            row = session.get(CV, uuid_module.UUID(cv_id))
            if row is None:
                return {"cv_id": cv_id, "status": "missing"}
            text = row.extracted_text or ""
            tenant = row.tenant_id
            criteria = row.criteria or {}
            # Carried into the payload so a hard filter runs *inside* Qdrant.
            # Filtering after the search would mean reading profiles we are
            # about to discard — the same argument that put tenant isolation
            # in the query rather than in Python.
            payload = {
                "filename": row.original_filename[:300],
                # The name a shortlist shows, resolved once here so a search
                # needs no round trip per profile to print a row.
                "label": (row.display_name or row.headline or row.original_filename)[:160],
                "technologies": criteria.get("technologies") or [],
                "languages": criteria.get("languages") or [],
                "education_level": criteria.get("education_level"),
                "certifications": criteria.get("certifications") or [],
            }

        try:
            outcome = _index_document(
                collection=get_settings().vector.cv_collection,
                owner_id=cv_id,
                tenant=tenant,
                text=text,
                extra_payload=payload,
            )
        except Exception as exc:
            logger.warning("indexing.cv_failed", error=str(exc)[:200])
            return {"cv_id": cv_id, "status": "failed", "error": str(exc)[:200]}

        logger.info("indexing.cv_indexed", **outcome)
        return {"cv_id": cv_id, **outcome}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.indexing.index_pending",
    queue="ai",
    max_retries=0,
)
def index_pending(self: PipelineTask, limit: int = 500) -> dict[str, Any]:
    """Index everything already extracted but never indexed.

    The same lesson the enrichment and CV backlogs taught: adding a step fixes
    what arrives next and does nothing for what is already stored. Idempotent,
    because point ids are derived — re-running it rewrites the same points.
    """
    with session_scope() as session:
        # Anything with matchable text, which is *not* the same as anything
        # with an attachment. 126 notices carry a rich publication and no
        # dossier at all — for the Tunisian corpus that publication averages
        # 19 783 characters and is the only text there is. Selecting on
        # `extraction_chars` alone silently skipped every one of them.
        from sqlalchemy import func, or_

        tenders = [
            str(row[0])
            for row in session.execute(
                select(Tender.id)
                .where(
                    or_(
                        Tender.extraction_chars > 0,
                        func.length(func.coalesce(Tender.description, "")) > 200,
                    )
                )
                .order_by(Tender.created_at.desc())
                .limit(limit)
            ).all()
        ]
        cvs = [
            str(row[0])
            for row in session.execute(
                select(CV.id).where(CV.extraction_status == "extracted").limit(limit)
            ).all()
        ]

    for tender_id in tenders:
        index_tender.apply_async(kwargs={"tender_id": tender_id}, queue="ai")
    for cv_id in cvs:
        index_cv.apply_async(kwargs={"cv_id": cv_id}, queue="ai")

    logger.info("indexing.backlog_dispatched", tenders=len(tenders), cvs=len(cvs))
    return {"tenders": len(tenders), "cvs": len(cvs)}
