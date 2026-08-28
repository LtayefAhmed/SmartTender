"""Rank CVs against a recruiter's own job posting — pasted or uploaded.

Standalone from the tender-matching endpoint: there is no ``Tender`` row here,
just text a recruiter hands over directly. Everything else is the same
dispatch-and-block pattern ``matching.py`` uses, because the reason for it is
unchanged — the embedding model must stay out of the API process.
"""

from __future__ import annotations

from typing import Any

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.api.deps import Principal, require_principal
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/job-match", tags=["job-match"])

#: Same budget as tender matching: a run is a handful of seconds, and a caller
#: left hanging longer than this learns less than one told plainly it timed out.
_TIMEOUT_SECONDS = 90


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@router.post("", summary="Rank CVs against a pasted or uploaded job posting")
async def match_job_posting(
    text: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    age_min: int | None = Form(default=None),
    age_max: int | None = Form(default=None),
    min_experience_years: int | None = Form(default=None),
    certifications: str | None = Form(default=None, description="Comma-separated."),
    education: str | None = Form(default=None, description="Comma-separated."),
    languages: str | None = Form(default=None, description="Comma-separated."),
    technologies: str | None = Form(default=None, description="Comma-separated."),
    limit: int = Form(default=20),
    requirements: int = Form(default=15),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Return the tenant's best-matching CVs for a job posting, with evidence.

    Accepts either pasted text or an uploaded PDF/DOCX, not both meaningfully
    at once — when both are given, the uploaded file's extracted text wins,
    since a browser client already prevents populating both and a
    non-browser caller needs a defined tie-break regardless.
    """
    has_text = bool(text and text.strip())
    if not has_text and file is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Provide job posting text or upload a file.",
        )

    job_text = text or ""
    if file is not None:
        from app.services.extraction import get_extractor
        from app.services.validation import UploadValidator

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
        job_text = extracted.text

    filters = {
        "age_min": age_min,
        "age_max": age_max,
        "min_experience_years": min_experience_years,
        "certifications": _split_list(certifications),
        "education": _split_list(education),
        "languages": _split_list(languages),
        "technologies": _split_list(technologies),
    }

    from app.workers.tasks.job_match import rank_job_posting_candidates

    task = rank_job_posting_candidates.apply_async(
        kwargs={
            "job_text": job_text,
            "tenant": principal.tenant,
            "filters": filters,
            "limit": limit,
            "requirement_limit": requirements,
        },
        queue="ai",
    )

    try:
        outcome = await anyio.to_thread.run_sync(
            lambda: task.get(timeout=_TIMEOUT_SECONDS, propagate=True)
        )
    except Exception as exc:
        logger.warning("api.job_match_failed", error=str(exc)[:200])
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Matching is unavailable. The embedding worker may be starting "
                "or the vector index may be unreachable."
            ),
        ) from exc

    logger.info(
        "api.job_match_completed",
        candidates=len(outcome.get("candidates", [])),
        actor=principal.identity,
    )
    return outcome
