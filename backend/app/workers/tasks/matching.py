"""The matching run, on the worker that owns the embedding model.

Kept out of the API for one reason: the encoder is 470 MB resident, and a
model loaded per request process is how indexing took the host down when it ran
on an eight-process queue. This task runs on ``ai``, served by a single worker,
so exactly one copy exists.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from app.core.logging import get_logger, log_context
from app.db.models.tender import Tender
from app.db.session import session_scope
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = ["rank_tender_candidates"]


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.matching.rank_tender_candidates",
    queue="ai",
    max_retries=0,
)
def rank_tender_candidates(
    self: PipelineTask,
    tender_id: str,
    tenant: str,
    limit: int = 20,
    requirement_limit: int = 15,
) -> dict[str, Any]:
    """Rank one organisation's candidates against one tender."""
    from app.services.chunking import chunk_text
    from app.services.matching import (
        MatchWeights,
        extract_requirements,
        match_tender,
        required_technologies,
    )
    from app.services.refinement import structure_requirements

    with log_context(tender_uuid=tender_id):
        with session_scope() as session:
            row = session.get(Tender, uuid_module.UUID(tender_id))
            if row is None:
                return {"tender_id": tender_id, "status": "missing", "candidates": []}
            title = row.title
            # Publication and dossier both carry requirements; for the notices
            # with no attachment the publication is all there is.
            text = "\n\n".join(part for part in (row.description, row.extracted_text) if part)

        if not text.strip():
            return {
                "tender_id": tender_id,
                "title": title,
                "status": "no_text",
                "message": "This tender has no readable text to match against.",
                "candidates": [],
            }

        chunks = chunk_text(text)
        requirements = extract_requirements(
            [(c.text, c.document, c.index, c.priority) for c in chunks],
            limit=requirement_limit,
        )
        wanted = required_technologies(text)
        weights = MatchWeights()

        # The curated lexicon is precise and finite; a dossier can demand
        # something nobody thought to list. Reading the requirement passages
        # with the model recovers those, and the union keeps the lexicon's
        # precision while gaining the recall.
        #
        # Best-effort throughout: no key, a timeout or a malformed answer
        # leaves `wanted` exactly as the lexicon produced it, and the run
        # proceeds. Structured requirements are an improvement, not a step the
        # matching depends on.
        structured = structure_requirements(
            "\n\n".join(r.text for r in requirements[:6]), kind="tender"
        )
        if structured:
            for term in structured.get("technologies", []):
                if term and term not in wanted:
                    wanted.append(term)

        # One run, sliced afterwards. Ranking twice to count the refusals would
        # double fifteen encodings and fifteen vector queries to learn a number
        # already in hand.
        considered = match_tender(
            tender_text=text,
            requirements=requirements,
            tenant=tenant,
            limit=100_000,
            veto_sample=100_000,
            required=wanted,
        )
        kept = [m for m in considered if not m.vetoed]
        refused = [m for m in considered if m.vetoed]
        kept_total, vetoed_total = len(kept), len(refused)

        # A refused profile scores zero and therefore always sorts last, so a
        # plain top-N slice never contains one. The interface reported "0
        # écartés" on a run where three hundred profiles had been floored —
        # reading as "the rule did nothing" when it did everything. The
        # shortlist and the evidence that it was filtered are two answers, and
        # the reader is owed both.
        matches = kept[:limit] + refused[:8]

        logger.info(
            "matching.completed",
            requirements=len(requirements),
            technologies=len(wanted),
            candidates=len(matches),
        )
        return {
            "tender_id": tender_id,
            "title": title,
            "status": "ok",
            # Returned so the interface can say what the ranking was based on.
            # A score without its question is a number a bid manager cannot
            # argue with, and arguing with it is exactly what they should do.
            "requirements": [
                {"position": r.position, "document": r.document, "text": r.text[:500]}
                for r in requirements
            ],
            "required_technologies": wanted,
            "kept_total": kept_total,
            "vetoed_total": vetoed_total,
            # What the model read out of the requirement passages, or null when
            # it was unavailable. Null and "nothing required" are different
            # facts and the interface must not merge them.
            "structured_requirements": structured,
            "weights": {"version": weights.version, **weights.as_dict()},
            "candidates": [match.to_dict() for match in matches],
        }
