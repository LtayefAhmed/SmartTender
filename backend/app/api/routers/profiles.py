"""Searching the CV base directly, and the vocabulary to search it with.

Two endpoints with one idea between them: a recruiter should never guess.

``/profiles/facets`` returns the criteria that actually occur in *their* base,
each with a count. Offering "Kubernetes" when nobody holds it produces an empty
result and no explanation; offering "Kubernetes (2)" tells the recruiter what
to expect before they click. Suggestions derived from a curated list would
promise things the corpus cannot deliver.

``/profiles/search`` ranks. The work happens on the ``ai`` worker, which owns
the embedding model — the same reason tender matching does.
"""

from __future__ import annotations

from typing import Any

import anyio
from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.core.logging import get_logger
from app.db.models.cv import CV
from app.workers.celery_app import TASK_PRIORITY

logger = get_logger(__name__)
router = APIRouter(prefix="/profiles", tags=["profiles"])

_TIMEOUT_SECONDS = 90


@router.get("/facets", summary="Criteria present in this organisation's CV base")
async def facets(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """What can be searched for, with how many profiles hold each.

    Counted over the base rather than read from a fixed vocabulary: a filter
    that returns nothing teaches a user to distrust the screen, and the count
    is what lets them avoid it before clicking.
    """
    rows = (
        await session.execute(
            select(CV.criteria).where(
                CV.tenant_id == principal.tenant,
                CV.extraction_status == "extracted",
            )
        )
    ).all()

    technologies: dict[str, int] = {}
    languages: dict[str, int] = {}
    certifications: dict[str, int] = {}
    education: dict[int, int] = {}

    for (criteria,) in rows:
        criteria = criteria or {}
        for value in criteria.get("technologies") or []:
            technologies[value] = technologies.get(value, 0) + 1
        for value in criteria.get("languages") or []:
            languages[value] = languages.get(value, 0) + 1
        for value in criteria.get("certifications") or []:
            certifications[value] = certifications.get(value, 0) + 1
        level = criteria.get("education_level")
        if isinstance(level, int):
            education[level] = education.get(level, 0) + 1

    def _ranked(counts: dict[Any, int]) -> list[dict[str, Any]]:
        # Most common first: a recruiter scanning a list wants the plausible
        # options at the top, not an alphabet.
        return [
            {"value": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
        ]

    return {
        "profiles": len(rows),
        "technologies": _ranked(technologies),
        "languages": _ranked(languages),
        "certifications": _ranked(certifications),
        # Cumulative: asking for Bac+3 must count everyone at Bac+5 too, or the
        # number beside the option contradicts what the filter does.
        "education": [
            {
                "value": level,
                "label": label,
                "count": sum(n for lvl, n in education.items() if lvl >= level),
            }
            for level, label in (
                (2, "Bac+2 et plus"),
                (3, "Bac+3 et plus"),
                (5, "Bac+5 et plus"),
                (8, "Doctorat"),
            )
        ],
    }


@router.post("/search", summary="Rank profiles against a query")
async def search(
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Rank this organisation's profiles. No tender involved."""
    from app.workers.tasks.profiles import search_profiles_task

    task = search_profiles_task.apply_async(
        kwargs={
            "tenant": principal.tenant,
            "text": str(payload.get("text") or "")[:20_000],
            "technologies": [str(v) for v in (payload.get("technologies") or [])][:20],
            "languages": [str(v) for v in (payload.get("languages") or [])][:10],
            "certifications": [str(v) for v in (payload.get("certifications") or [])][:10],
            "education_min": payload.get("education_min"),
            "limit": min(int(payload.get("limit") or 20), 100),
        },
        queue="ai",
        priority=TASK_PRIORITY["interactive"],
    )

    try:
        outcome = await anyio.to_thread.run_sync(
            lambda: task.get(timeout=_TIMEOUT_SECONDS, propagate=True)
        )
    except Exception as exc:
        logger.warning("api.profile_search_failed", error=str(exc)[:200])
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "La recherche est indisponible. Le worker d'embedding démarre "
                "peut-être, ou l'index vectoriel est injoignable."
            ),
        ) from exc

    logger.info(
        "api.profile_search",
        results=len(outcome.get("results", [])),
        actor=principal.identity,
    )
    return outcome


@router.post("/job-description", summary="Read a job description into search filters")
async def analyse_job_description(
    file: UploadFile = File(..., description="PDF ou DOCX de la fiche de poste."),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Extract a job description and propose filters from it.

    The filters come back for the recruiter to *see and correct*, never applied
    silently. A model reading a fiche de poste will miss a requirement or read
    one that is not there, and a search running on an unseen interpretation
    produces an answer nobody can question.
    """
    from app.services.extraction import get_extractor
    from app.services.validation import UploadValidator
    from app.workers.tasks.profiles import structure_job_description

    validator = UploadValidator()
    content = await anyio.to_thread.run_sync(
        lambda: validator.read_stream(file.file, declared_size=file.size)
    )
    validated = await anyio.to_thread.run_sync(
        lambda: validator.validate(
            content, filename=file.filename, declared_content_type=file.content_type
        )
    )

    extracted = await anyio.to_thread.run_sync(
        lambda: get_extractor().extract(
            validated.content,
            content_type=validated.content_type,
            filename=validated.filename,
        )
    )
    if not extracted.ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=extracted.error or "Aucun texte lisible dans ce document.",
        )

    task = structure_job_description.apply_async(
        kwargs={"text": extracted.text[:20_000]},
        queue="ai",
        priority=TASK_PRIORITY["interactive"],
    )
    try:
        outcome = await anyio.to_thread.run_sync(
            lambda: task.get(timeout=_TIMEOUT_SECONDS, propagate=True)
        )
    except Exception as exc:
        logger.warning("api.job_description_failed", error=str(exc)[:200])
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="L'analyse est indisponible pour le moment.",
        ) from exc

    logger.info(
        "api.job_description_read",
        filename=validated.filename,
        chars=len(extracted.text),
        actor=principal.identity,
    )
    return {**outcome, "filename": validated.filename, "text": extracted.text[:4000]}
