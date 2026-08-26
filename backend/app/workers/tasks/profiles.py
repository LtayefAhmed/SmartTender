"""Profile search, on the worker that owns the embedding model.

Same reason as tender matching: the encoder is 470 MB resident, and one copy
per API process is what took the host down when indexing ran on a
high-concurrency queue.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger, log_context
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = ["search_profiles_task", "structure_job_description"]


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.profiles.search_profiles_task",
    queue="ai",
    max_retries=0,
)
def search_profiles_task(
    self: PipelineTask,
    tenant: str,
    text: str = "",
    technologies: list[str] | None = None,
    languages: list[str] | None = None,
    certifications: list[str] | None = None,
    education_min: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Rank profiles against a free-text and filter query."""
    from app.services.profile_search import ProfileQuery, SearchWeights, search_profiles

    with log_context(tenant=tenant):
        query = ProfileQuery(
            text=text,
            technologies=technologies or [],
            languages=languages or [],
            certifications=certifications or [],
            education_min=int(education_min) if education_min else None,
        )
        if query.is_empty():
            return {"status": "empty", "results": [], "total": 0}

        weights = SearchWeights()
        hits, total = search_profiles(query, tenant=tenant, limit=limit)

        logger.info("profiles.searched", total=total, returned=len(hits))
        return {
            "status": "ok",
            # Total *before* the limit: twenty results out of two hundred and
            # twenty results out of twenty are different answers, and a
            # recruiter deciding whether to narrow their query needs to know
            # which one they are looking at.
            "total": total,
            "weights": {
                "version": weights.version,
                "semantic": weights.semantic,
                "languages": weights.languages,
                "education": weights.education,
                "certifications": weights.certifications,
            },
            "query": {
                "text": text,
                "technologies": query.technologies,
                "languages": query.languages,
                "certifications": query.certifications,
                "education_min": query.education_min,
            },
            "results": [hit.to_dict() for hit in hits],
        }


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.profiles.structure_job_description",
    queue="ai",
    max_retries=0,
)
def structure_job_description(self: PipelineTask, text: str) -> dict[str, Any]:
    """Read a job description into search filters.

    The filters are handed back for the recruiter to *correct*, not applied
    silently. A model reading a fiche de poste will miss a requirement or
    invent one, and a search that runs on an unseen interpretation gives an
    answer nobody can question.
    """
    from app.services.cv_criteria import normalise_language
    from app.services.matching import required_technologies
    from app.services.refinement import structure_requirements

    if not text or len(text.strip()) < 40:
        return {"status": "too_short", "technologies": [], "languages": []}

    # The lexicon runs whatever the model does: it is precise, free, and
    # unaffected by a missing key or an exhausted quota.
    technologies = required_technologies(text)

    structured = structure_requirements(text, kind="tender")
    languages: list[str] = []
    certifications: list[str] = []
    education_min: int | None = None

    if structured:
        for term in structured.get("technologies", []):
            if term and term not in technologies:
                technologies.append(term)
        for raw in structured.get("langues", []):
            canonical = normalise_language(raw)
            if canonical and canonical not in languages:
                languages.append(canonical)
        certifications = list(structured.get("certifications", []))[:8]
        years = structured.get("experience_min_annees")
        _ = years  # years of experience is not a diploma; kept out on purpose

    from app.services.cv_criteria import extract_criteria

    # The diploma is read with the same rule that reads it from a CV, so a
    # search for "Bac+5" means on both sides exactly what it says.
    education_min = extract_criteria(text).education_level

    logger.info(
        "profiles.job_description_structured",
        technologies=len(technologies),
        languages=len(languages),
        llm_used=bool(structured),
    )
    return {
        "status": "ok",
        "llm_used": bool(structured),
        "technologies": technologies[:20],
        "languages": languages,
        "certifications": certifications,
        "education_min": education_min,
    }
