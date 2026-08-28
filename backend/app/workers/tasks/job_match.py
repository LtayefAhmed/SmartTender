"""Ranking imported CVs against a recruiter's own job posting.

This branch references the later vector-matching stack, but those service
modules are not present. The task therefore uses the existing local services:
read imported CV files from storage, extract text, and rank them with the
configured similarity backend.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger, log_context
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = ["rank_job_posting_candidates"]


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.job_match.rank_job_posting_candidates",
    queue="ai",
    max_retries=0,
)
def rank_job_posting_candidates(
    self: PipelineTask,
    job_text: str,
    tenant: str,
    filters: dict[str, Any],
    limit: int = 20,
    requirement_limit: int = 15,
) -> dict[str, Any]:
    """Rank imported CVs against a job posting supplied as raw text."""
    from sqlalchemy import select

    from app.core.identity import normalize_text
    from app.db.models.cv import CV
    from app.db.session import session_scope
    from app.services.extraction import get_extractor
    from app.services.similarity import get_similarity_backend
    from app.services.storage import get_storage

    del self, requirement_limit

    with log_context(tenant=tenant):
        if not job_text.strip():
            return {
                "status": "no_text",
                "message": "No job posting text to match against.",
                "candidates": [],
            }

        wanted = _string_items(filters.get("technologies"))
        wanted_normalized = [(item, normalize_text(item)) for item in wanted]
        backend = get_similarity_backend()
        extractor = get_extractor()
        storage = get_storage()

        with session_scope() as session:
            rows = (
                session.execute(
                    select(CV).where((CV.uploaded_by == tenant) | (CV.uploaded_by.is_(None)))
                )
                .scalars()
                .all()
            )

        ranked: list[tuple[float, CV, str, list[str], list[str]]] = []
        for row in rows:
            cv_text = ""
            try:
                data = storage.get_bytes(row.storage_key)
                extracted = extractor.extract(
                    data,
                    content_type=row.content_type,
                    filename=row.original_filename,
                )
                cv_text = extracted.text or ""
            except Exception as exc:
                logger.warning(
                    "job_match.cv_text_unavailable",
                    cv_id=str(row.id),
                    error=str(exc)[:200],
                )

            normalized_cv = normalize_text(cv_text)
            matched = [
                label
                for label, normalized in wanted_normalized
                if normalized and normalized in normalized_cv
            ]
            missing = [label for label, _ in wanted_normalized if label not in matched]
            score = backend.similarity(job_text, cv_text)
            if wanted:
                score = (score * 0.75) + ((len(matched) / len(wanted)) * 0.25)
            ranked.append((score, row, cv_text, matched, missing))

        ranked.sort(key=lambda item: item[0], reverse=True)

        candidates: list[dict[str, Any]] = []
        for score, row, cv_text, matched, missing in ranked[:limit]:
            passage = cv_text.strip().replace("\n", " ")[:500]
            candidates.append(
                {
                    "cv_id": str(row.id),
                    "label": row.original_filename,
                    "headline": row.source_url,
                    "score": round(score, 4),
                    "retrieval_score": round(score, 4),
                    "matched_technologies": matched,
                    "missing_technologies": missing,
                    "evidence": [
                        {
                            "passage": passage,
                            "document": row.original_filename,
                            "score": round(score, 4),
                        }
                    ]
                    if passage
                    else [],
                    "vetoed": False,
                    "veto_reason": None,
                    "filtered_out": False,
                    "filtered_reason": None,
                    "structured_profile": {
                        "age": None,
                        "experience_years": None,
                        "education": None,
                        "certifications": [],
                        "languages": [],
                        "skills": [],
                    },
                }
            )

        logger.info(
            "job_match.completed",
            technologies=len(wanted),
            candidates=len(candidates),
        )
        return {
            "status": "ok",
            "requirements": [{"position": 0, "document": None, "text": job_text[:500]}],
            "required_technologies": wanted,
            "kept_total": len(candidates),
            "vetoed_total": 0,
            "filtered_total": 0,
            "filters_applied": filters,
            "structured_requirements": None,
            "weights": {"version": "lexical-fallback"},
            "candidates": candidates,
        }
