"""Reading a CV's own facts — age, experience, certifications — out of its text.

The counterpart to ``app.services.refinement.structure_requirements``, which
reads what a *tender* demands. This reads what a *candidate* states. Kept in
its own module rather than added to ``refinement.py``: the two read different
kinds of document for different callers, and nothing is gained by coupling
them beyond the prompt-construction pattern, which is copied rather than
shared.

Extraction is cached in ``CVProfile`` (one row per CV) because reading every
CV in a search's semantic top-N with a fresh model call, every search, would
turn a five-second ranking into a slow and costly one for no benefit — a CV's
stated age does not change between two recruiters running two searches an
hour apart.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.core.identity import normalize_text
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "JobMatchFilters",
    "apply_filters",
    "get_cv_profiles",
    "structure_cv_profile",
]

_CV_SYSTEM = (
    "Tu lis un CV. Extrais uniquement ce que le document DIT explicitement sur "
    "la personne. Reponds en JSON strict, sans texte autour, avec ce schema :\n"
    '{"age": null, "experience_years": null, "certifications": [], '
    '"education": null, "languages": [], "skills": []}\n'
    "age : nombre d'annees, uniquement si un age ou une date de naissance est "
    "donnee explicitement. "
    "experience_years : nombre total d'annees d'experience professionnelle, "
    "en nombre entier. "
    "certifications : certifications ou habilitations nommees (ex: PMP, "
    "AWS Certified, ITIL), une chaine courte par entree. "
    "education : le diplome ou niveau d'etudes le plus eleve, en une courte "
    "chaine (ex: 'Ingenieur', 'Master', 'Bac+5'), ou null. "
    "languages : langues parlees ou ecrites mentionnees, une par entree. "
    "skills : competences ou technologies nommees, une chaine courte par entree. "
    "N'invente rien : un champ sans information reste vide ou null."
)

#: Below this, forcing a call teaches nothing — same threshold
#: ``structure_requirements`` uses for a tender passage.
_MIN_CHARS = 80

#: A model's own arithmetic error is worth showing; a hallucinated number
#: outside any plausible range is worse than none. Neither ``refine_ocr_text``
#: nor ``structure_requirements`` needs a comparable guard because neither has
#: a numeric field a model can invent so cheaply.
_AGE_RANGE = (16, 75)
_EXPERIENCE_RANGE = (0, 50)

#: Keys a model reaches for when it answers with an object where a string was
#: asked for. Duplicated from ``app.services.refinement._as_strings`` rather
#: than imported, so the two modules stay independent.
_LABEL_KEYS = ("intitule", "intitulé", "nom", "name", "label", "titre")


def _as_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            label = next((str(item[k]) for k in _LABEL_KEYS if item.get(k)), "")
        else:
            label = str(item)
        label = label.strip()
        if label and len(label) <= 120 and label not in out:
            out.append(label)
    return out


def _as_bounded_int(value: Any, bounds: tuple[int, int]) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    low, high = bounds
    if value < low or value > high:
        return None
    return value


def structure_cv_profile(
    text: str, *, known_names: list[str] | None = None
) -> dict[str, Any] | None:
    """Read a CV's own stated facts into fields. ``None`` when unavailable.

    ``None`` rather than an empty structure keeps "the model was off"
    distinguishable from "this CV states nothing" — the same distinction
    ``structure_requirements`` preserves, for the same reason: one is worth
    retrying later, the other is not.
    """
    from app.services.llm import get_llm

    if not text or len(text.strip()) < _MIN_CHARS:
        return None

    result = get_llm().complete(
        system=_CV_SYSTEM,
        user=text,
        kind="cv",
        known_names=known_names,
        max_tokens=700,
    )
    parsed = result.as_json()
    if not isinstance(parsed, dict):
        return None

    return {
        "age": _as_bounded_int(parsed.get("age"), _AGE_RANGE),
        "experience_years": _as_bounded_int(parsed.get("experience_years"), _EXPERIENCE_RANGE),
        "education": (str(parsed["education"]).strip()[:160] or None)
        if parsed.get("education")
        else None,
        "certifications": _as_strings(parsed.get("certifications")),
        "languages": _as_strings(parsed.get("languages")),
        "skills": _as_strings(parsed.get("skills")),
    }


def get_cv_profiles(cv_ids: list[str], tenant: str) -> dict[str, dict[str, Any]]:
    """Structured profile for each id, extracting and caching what is missing.

    Called only on a search's semantic top-N — never the vetoed sample, never
    the tenant's whole CV corpus — which is what bounds the LLM cost to a
    search's actual shortlist rather than the size of the database.

    Always returns one entry per requested id (an empty-fielded profile when
    extraction was unavailable, short, or the CV vanished) — never raises,
    never leaves a caller checking for a missing key.
    """
    import uuid as uuid_module

    from sqlalchemy import select

    from app.db.models.cv import CV
    from app.db.models.cv_profile import CVProfile
    from app.db.session import session_scope

    empty: dict[str, Any] = {
        "age": None,
        "experience_years": None,
        "education": None,
        "certifications": [],
        "languages": [],
        "skills": [],
    }

    try:
        identifiers = [uuid_module.UUID(value) for value in cv_ids]
    except ValueError:
        return {}

    if not identifiers:
        return {}

    with session_scope() as session:
        cv_rows = session.execute(
            select(CV.id, CV.tenant_id, CV.extracted_text, CV.display_name).where(
                CV.id.in_(identifiers)
            )
        ).all()
        # Defence in depth: `cv_ids` already passed through a tenant-scoped
        # vector search upstream, but this is the last point before structured
        # data leaves the function, and CV data is the isolation-sensitive case
        # `CV.owned_by` exists for.
        cv_by_id = {str(row[0]): row for row in cv_rows if row[1] == tenant}

        cached_rows = session.execute(
            select(CVProfile).where(CVProfile.cv_id.in_(identifiers))
        ).scalars().all()
        cached_by_id = {str(row.cv_id): row for row in cached_rows}

        profiles: dict[str, dict[str, Any]] = {}
        for cv_id in cv_ids:
            cv_row = cv_by_id.get(cv_id)
            if cv_row is None:
                profiles[cv_id] = dict(empty)
                continue

            _id, _tenant, extracted_text, display_name = cv_row
            text = extracted_text or ""
            source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None

            cached = cached_by_id.get(cv_id)
            if cached is not None and cached.status == "ok" and cached.source_hash == source_hash:
                profiles[cv_id] = {
                    "age": cached.age,
                    "experience_years": cached.experience_years,
                    "education": cached.education,
                    "certifications": cached.certifications,
                    "languages": cached.languages,
                    "skills": cached.skills,
                }
                continue

            known_names = [display_name] if display_name else None
            structured = structure_cv_profile(text, known_names=known_names)

            if cached is None:
                cached = CVProfile(cv_id=uuid_module.UUID(cv_id))
                session.add(cached)

            cached.source_hash = source_hash
            cached.source_chars = len(text)
            cached.raw_extraction = structured
            if structured is not None:
                cached.status = "ok"
                cached.error = None
                cached.age = structured["age"]
                cached.experience_years = structured["experience_years"]
                cached.education = structured["education"]
                cached.certifications = structured["certifications"]
                cached.languages = structured["languages"]
                cached.skills = structured["skills"]
                profiles[cv_id] = {k: structured[k] for k in empty}
            else:
                cached.status = "empty" if len(text.strip()) < _MIN_CHARS else "unavailable"
                cached.age = None
                cached.experience_years = None
                cached.education = None
                cached.certifications = []
                cached.languages = []
                cached.skills = []
                profiles[cv_id] = dict(empty)

        return profiles


@dataclass(slots=True)
class JobMatchFilters:
    """What a recruiter is asking of the shortlist, beyond semantic fit."""

    age_min: int | None = None
    age_max: int | None = None
    min_experience_years: int | None = None
    certifications: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "age_min": self.age_min,
            "age_max": self.age_max,
            "min_experience_years": self.min_experience_years,
            "certifications": self.certifications,
            "education": self.education,
            "languages": self.languages,
            "technologies": self.technologies,
        }


def _any_match(wanted: list[str], have: list[str]) -> bool:
    if not wanted:
        return True
    have_normalized = {normalize_text(item) for item in have}
    return any(normalize_text(item) in have_normalized for item in wanted)


def apply_filters(
    profile: dict[str, Any], filters: JobMatchFilters
) -> tuple[bool, str | None]:
    """Whether a candidate's structured profile satisfies the filters.

    Missing data never rejects — a filter cannot fail against "unknown", it
    can only fail against a stated value that disagrees. Every filter type
    uses any-of matching: a recruiter listing several acceptable
    certifications, languages or technologies almost always means "any one of
    these suffices," not "all of them."

    Never drops a candidate itself — returns the verdict and, on rejection, a
    short reason naming exactly which filter failed, mirroring
    ``CandidateMatch``'s ``vetoed``/``veto_reason`` so a shortlist stays
    auditable rather than silently smaller.
    """
    age = profile.get("age")
    if age is not None:
        if filters.age_min is not None and age < filters.age_min:
            return False, f"age {age} < {filters.age_min}"
        if filters.age_max is not None and age > filters.age_max:
            return False, f"age {age} > {filters.age_max}"

    experience = profile.get("experience_years")
    if (
        experience is not None
        and filters.min_experience_years is not None
        and experience < filters.min_experience_years
    ):
        return False, f"experience {experience} ans < {filters.min_experience_years} requis"

    # Each of these rejects only when the profile *states* something and it
    # disagrees with every listed value — an empty profile field is "unknown",
    # not "none", and unknown must not become a rejection.
    certifications = profile.get("certifications") or []
    if filters.certifications and certifications and not _any_match(
        filters.certifications, certifications
    ):
        return False, "aucune certification demandee trouvee"

    education = profile.get("education")
    if filters.education and education and not _any_match(filters.education, [education]):
        return False, "niveau d'etudes demande non trouve"

    languages = profile.get("languages") or []
    if filters.languages and languages and not _any_match(filters.languages, languages):
        return False, "aucune langue demandee trouvee"

    skills = profile.get("skills") or []
    if filters.technologies and skills and not _any_match(filters.technologies, skills):
        return False, "aucune technologie demandee trouvee"

    return True, None
