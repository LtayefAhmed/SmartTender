"""The referential of validated responses — the platform's memory of its own work.

The parcours calls for a dedicated Qdrant collection of "réponses passées
validées — style et faits éprouvés, récupérés par similarité avec l'AO
courant". This is it, and it closes the only loop in the whole design that
feeds back into itself:

    a dossier is approved  →  its text is indexed here
                           →  retrieved when the next tender resembles it
                           →  offered to the model as *style*
                           →  the next dossier is written better

Three rules, and each exists because the loop is also the easiest place to do
real damage.

**Only approved documents enter.** A draft is an opinion; an approved dossier
is something a person signed and a client received. Indexing drafts would feed
the model text nobody stood behind, and the model has no way to tell the
difference afterwards.

**Retrieved passages are style, never facts.** A past response describes *other
people's* work on *another* mission. Copying a fact from it into a new CV is
not reuse, it is invention with extra steps — so the prompt says so explicitly,
and the anti-invention guard still checks every reformulation against the
consultant's own CV, not against what was retrieved.

**Empty is the correct initial state.** The collection cannot predate the first
validation, so everything downstream degrades to "no examples retrieved" and
produces the same document it produced before. The loop pays from the second
cycle, not the first, and pretending otherwise would mean seeding it with
something unvalidated.
"""

from __future__ import annotations

import uuid as uuid_module
from dataclasses import dataclass

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["RetrievedResponse", "index_response", "recall_responses"]

#: Passages offered to a prompt. Three is enough to carry a house style and
#: small enough that the token cap still leaves room for the CV itself — which
#: is the part that must not be squeezed, because it is the only source the
#: anti-invention guard can check against.
_RECALL_LIMIT = 3

#: Below this, a retrieved passage is about something else. A referential that
#: offers an irrelevant example is worse than one that offers none: it nudges
#: the wording of a bid toward a mission it has nothing to do with.
_MIN_SCORE = 0.35


@dataclass(slots=True)
class RetrievedResponse:
    text: str
    score: float
    tender_title: str | None
    document_label: str | None


def _point_id(document_id: str, position: int) -> str:
    return str(uuid_module.uuid5(uuid_module.NAMESPACE_URL, f"{document_id}:{position}"))


def index_response(
    *,
    document_id: str,
    text: str,
    tenant: str,
    kind: str,
    tender_title: str | None = None,
    label: str | None = None,
) -> int:
    """Add an approved document to the referential.

    Returns the number of passages written. Failure is logged and swallowed:
    capitalisation is derived data, and losing it must never undo an approval
    a human just granted.
    """
    if not text or not text.strip():
        return 0

    try:
        from app.services.chunking import chunk_text
        from app.services.embeddings import get_embedder
        from app.services.vectors import VectorPoint, get_vector_store

        chunks = chunk_text(text)
        if not chunks:
            return 0

        embedder = get_embedder()
        store = get_vector_store()
        vectors = embedder.encode_many([chunk.text for chunk in chunks])

        points = [
            VectorPoint(
                # Deterministic: re-indexing the same document overwrites its
                # passages instead of duplicating them.
                id=_point_id(document_id, chunk.index),
                vector=vector,
                payload={
                    "tenant_id": tenant,
                    "owner_id": document_id,
                    "kind": kind,
                    "tender_title": tender_title or "",
                    "label": label or "",
                    "position": chunk.index,
                    "text": chunk.text,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        written = store.upsert(
            store.response_collection, points, dimensions=embedder.dimensions
        )
    except Exception as exc:
        logger.warning("memory.index_failed", document_id=document_id, error=str(exc)[:200])
        return 0

    logger.info("memory.response_indexed", document_id=document_id, passages=written, kind=kind)
    return written


def recall_responses(
    query: str, *, tenant: str, limit: int = _RECALL_LIMIT
) -> list[RetrievedResponse]:
    """Passages of past approved dossiers that resemble this tender.

    Returns an empty list for every failure mode — no collection, no vectors,
    an unreachable store — because the only correct reaction is to write the
    document without examples.
    """
    if not query or not query.strip():
        return []

    try:
        from app.services.embeddings import get_embedder
        from app.services.vectors import get_vector_store

        store = get_vector_store()
        embedder = get_embedder()
        hits = store.search(
            store.response_collection,
            embedder.encode(query),
            tenant=tenant,
            limit=limit,
            min_score=_MIN_SCORE,
        )
    except Exception as exc:
        logger.info("memory.recall_unavailable", error=str(exc)[:200])
        return []

    found = [
        RetrievedResponse(
            text=hit.text,
            score=hit.score,
            tender_title=(hit.payload.get("tender_title") or None),
            document_label=(hit.payload.get("label") or None),
        )
        for hit in hits
    ]
    logger.info("memory.recalled", found=len(found))
    return found
