"""Ranking CVs against a recruiter's own job posting, not a scraped tender.

Runs on the ``ai`` worker for the same reason ``matching.py`` does: the
embedding model is 470 MB resident, and one worker at concurrency 1 must own
the only copy. Mirrors ``rank_tender_candidates`` step for step, with two
differences: the job text is supplied directly instead of loaded from a
``Tender`` row, and the semantic top-N is further checked against the
recruiter's structured filters (age, experience, certifications, ...) before
being returned.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger, log_context
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = ["rank_job_posting_candidates"]


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
    """Rank the tenant's CVs against a job posting supplied as raw text."""
    from app.services.chunking import chunk_text
    from app.services.cv_profile import JobMatchFilters, apply_filters, get_cv_profiles
    from app.services.matching import (
        MatchWeights,
        Requirement,
        extract_requirements,
        match_tender,
        required_technologies,
    )
    from app.services.refinement import structure_requirements

    with log_context(tenant=tenant):
        if not job_text.strip():
            return {
                "status": "no_text",
                "message": "No job posting text to match against.",
                "candidates": [],
            }

        chunks = chunk_text(job_text)
        requirements = extract_requirements(
            [(c.text, c.document, c.index, c.priority) for c in chunks],
            limit=requirement_limit,
        )
        if not requirements:
            # extract_requirements's length and obligation-language heuristics
            # are tuned to pick the few substantive passages out of a large,
            # mostly-boilerplate scraped dossier — reasonable when filtering a
            # multi-page CCTP, wrong for a recruiter's own input. There is no
            # boilerplate to filter here: whatever was typed or extracted from
            # an uploaded file *is* the whole posting, so even a two-word
            # search like "Comptable senior" is entirely signal and must
            # still produce something to search against — not a silently
            # empty shortlist for input a human plainly wrote with intent.
            # `job_text` is guaranteed non-empty at this point (the earlier
            # guard above already handled the truly-blank case).
            requirements = [Requirement(text=job_text.strip(), document=None, position=0)]
        wanted = required_technologies(job_text)
        weights = MatchWeights()

        # Job posting text is not personal data, so this reads it the same way
        # a tender's requirement passages are read — no CV-scope check needed.
        structured = structure_requirements(
            "\n\n".join(r.text for r in requirements[:6]), kind="tender"
        )
        if structured:
            for term in structured.get("technologies", []):
                if term and term not in wanted:
                    wanted.append(term)

        considered = match_tender(
            tender_text=job_text,
            requirements=requirements,
            tenant=tenant,
            limit=100_000,
            veto_sample=100_000,
            required=wanted,
        )
        kept = [m for m in considered if not m.vetoed]
        refused = [m for m in considered if m.vetoed]
        kept_total, vetoed_total = len(kept), len(refused)

        shortlisted = kept[:limit]
        job_filters = JobMatchFilters(**filters)
        profiles = get_cv_profiles([m.cv_id for m in shortlisted], tenant=tenant)

        candidates: list[dict[str, Any]] = []
        filtered_total = 0
        for match in shortlisted:
            profile = profiles.get(match.cv_id)
            payload = match.to_dict()
            if profile is None:
                payload["filtered_out"] = None
                payload["filtered_reason"] = None
                payload["structured_profile"] = None
            else:
                passed, reason = apply_filters(profile, job_filters)
                payload["filtered_out"] = not passed
                payload["filtered_reason"] = reason
                payload["structured_profile"] = profile
                if not passed:
                    filtered_total += 1
            candidates.append(payload)

        for match in refused[:8]:
            payload = match.to_dict()
            payload["filtered_out"] = None
            payload["filtered_reason"] = None
            payload["structured_profile"] = None
            candidates.append(payload)

        logger.info(
            "job_match.completed",
            requirements=len(requirements),
            technologies=len(wanted),
            candidates=len(candidates),
            filtered_total=filtered_total,
        )
        return {
            "status": "ok",
            "requirements": [
                {"position": r.position, "document": r.document, "text": r.text[:500]}
                for r in requirements
            ],
            "required_technologies": wanted,
            "kept_total": kept_total,
            "vetoed_total": vetoed_total,
            "filtered_total": filtered_total,
            "filters_applied": job_filters.as_dict(),
            "structured_requirements": structured,
            "weights": {"version": weights.version, **weights.as_dict()},
            "candidates": candidates,
        }
