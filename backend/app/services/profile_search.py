"""Searching the CV base directly, without a tender.

The tender matcher answers "who fits *this* dossier". This answers "who do we
have who does X" — a recruiter with a role to staff and no notice in hand.

The two look alike and differ on one point that changes the rules. A tender's
technology list is **inferred** from a document: twenty-four terms read out of
a CCTP, where matching one is chance, which is why that veto scales with the
count. A search's technology list is **chosen**: a recruiter who ticked Java,
Spring and Kubernetes meant all three, and returning someone who has only Java
answers a question they did not ask.

So here technologies are required, every one of them. Languages, diplomas and
certifications are not — measured over 344 CVs, 21% state a language and 10% a
recognised certification. Filtering hard on those would reject a candidate for
a silence rather than for an absence, and the base would look empty when it is
merely quiet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["ProfileHit", "ProfileQuery", "SearchWeights", "search_profiles"]

#: Mirrors the labels the extractor assigns, so a level shown on a search row
#: reads exactly as it does on the profile it came from.
_EDUCATION_LABELS = {
    8: "Doctorat",
    5: "Bac+5 (Master / Ingénieur)",
    3: "Bac+3 (Licence / Bachelor)",
    2: "Bac+2 (BTS / DUT)",
}


@dataclass(slots=True)
class ProfileQuery:
    """What the recruiter asked for."""

    #: Free text, or the content of a job description. Optional: a search made
    #: only of filters is legitimate and builds its own query text below.
    text: str = ""
    #: Required. Every one of them.
    technologies: list[str] = field(default_factory=list)
    #: Wanted. Their absence lowers a rank, never removes a profile.
    languages: list[str] = field(default_factory=list)
    education_min: int | None = None
    certifications: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            (self.text.strip(), self.technologies, self.languages,
             self.education_min, self.certifications)
        )

    def as_query_text(self) -> str:
        """The text handed to the encoder.

        Filters are folded into it rather than used only as filters. A search
        for "Java, Spring, anglais" with no prose still has to rank the
        profiles that pass, and their order should follow how central those
        skills are to a CV rather than the order the index returns them in.
        """
        parts = [self.text.strip()]
        parts.extend(self.technologies)
        parts.extend(self.certifications)
        if self.languages:
            parts.append("langues : " + ", ".join(self.languages))
        if self.education_min:
            parts.append(f"niveau bac+{self.education_min}")
        return " ".join(part for part in parts if part).strip()


@dataclass(slots=True)
class SearchWeights:
    """How the signals combine.

    A criterion the recruiter did not ask for contributes nothing and its
    weight is redistributed to the semantic score — otherwise a search on
    technologies alone would be capped at 0.55 and every result would look
    mediocre.
    """

    version: str = "search-v1"
    semantic: float = 0.55
    languages: float = 0.15
    education: float = 0.15
    certifications: float = 0.15


@dataclass(slots=True)
class ProfileHit:
    cv_id: str
    label: str
    filename: str
    score: float
    similarity: float
    matched_technologies: list[str]
    matched_languages: list[str]
    missing_languages: list[str]
    matched_certifications: list[str]
    education_level: int | None
    education_label: str | None
    meets_education: bool
    evidence: list[tuple[float, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cv_id": self.cv_id,
            "label": self.label,
            "filename": self.filename,
            "score": round(self.score, 4),
            "similarity": round(self.similarity, 4),
            "matched_technologies": self.matched_technologies,
            "matched_languages": self.matched_languages,
            "missing_languages": self.missing_languages,
            "matched_certifications": self.matched_certifications,
            "education_level": self.education_level,
            "education_label": self.education_label,
            "meets_education": self.meets_education,
            "evidence": [
                {"score": round(score, 4), "passage": passage[:400]}
                for score, passage in self.evidence
            ],
        }


def search_profiles(
    query: ProfileQuery,
    *,
    tenant: str,
    limit: int = 20,
    weights: SearchWeights | None = None,
) -> tuple[list[ProfileHit], int]:
    """Rank the organisation's profiles against a query.

    Returns the hits and how many profiles passed the technology requirement,
    which is not the same number: a recruiter seeing twenty results wants to
    know whether that is all of them or the first twenty of two hundred.
    """
    from app.core.config import get_settings
    from app.services.embeddings import get_embedder
    from app.services.vectors import get_vector_store

    weights = weights or SearchWeights()
    if query.is_empty():
        return [], 0

    text = query.as_query_text()
    embedder = get_embedder()
    store = get_vector_store()
    collection = get_settings().vector.cv_collection

    hits = store.search(
        collection,
        embedder.encode(text),
        tenant=tenant,
        # Generous: the filtering below is what selects, and a top-k that cuts
        # before the criteria are applied would hide qualified profiles behind
        # semantically noisier ones.
        limit=2_000,
        min_score=0.0,
    )

    best: dict[str, Any] = {}
    for hit in hits:
        owner = str(hit.payload.get("owner_id") or "")
        if not owner:
            continue
        entry = best.setdefault(owner, {"payload": hit.payload, "passages": []})
        entry["passages"].append((hit.score, hit.text))

    results: list[ProfileHit] = []
    for cv_id, entry in best.items():
        payload = entry["payload"]
        held = {str(t).lower() for t in (payload.get("technologies") or [])}

        # The hard requirement. Every ticked technology, not one of them: the
        # recruiter chose this list deliberately, unlike a tender's, which is
        # read out of a document.
        wanted = [t for t in query.technologies if t]
        if wanted and not all(t.lower() in held for t in wanted):
            continue

        passages = sorted(entry["passages"], key=lambda item: -item[0])[:5]
        similarity = passages[0][0] if passages else 0.0

        languages = {str(v).lower() for v in (payload.get("languages") or [])}
        matched_languages = [v for v in query.languages if v.lower() in languages]
        missing_languages = [v for v in query.languages if v.lower() not in languages]

        certifications = {str(v).lower() for v in (payload.get("certifications") or [])}
        matched_certifications = [
            v for v in query.certifications if v.lower() in certifications
        ]

        level = payload.get("education_level")
        level = int(level) if isinstance(level, int) else None
        meets_education = (
            query.education_min is None
            or (level is not None and level >= query.education_min)
        )

        score, _ = _combine(
            weights=weights,
            query=query,
            similarity=similarity,
            language_ratio=(
                len(matched_languages) / len(query.languages) if query.languages else 0.0
            ),
            certification_ratio=(
                len(matched_certifications) / len(query.certifications)
                if query.certifications
                else 0.0
            ),
            meets_education=meets_education,
        )

        results.append(
            ProfileHit(
                cv_id=cv_id,
                label=str(payload.get("label") or payload.get("filename") or cv_id),
                filename=str(payload.get("filename") or ""),
                score=score,
                similarity=similarity,
                matched_technologies=wanted,
                matched_languages=matched_languages,
                missing_languages=missing_languages,
                matched_certifications=matched_certifications,
                education_level=level,
                education_label=_EDUCATION_LABELS.get(level) if level else None,
                meets_education=meets_education,
                evidence=passages,
            )
        )

    results.sort(key=lambda hit: (-hit.score, hit.filename))
    return results[:limit], len(results)


def _combine(
    *,
    weights: SearchWeights,
    query: ProfileQuery,
    similarity: float,
    language_ratio: float,
    certification_ratio: float,
    meets_education: bool,
) -> tuple[float, dict[str, float]]:
    """Weighted score, with unasked criteria folded back into the semantic part.

    Leaving an unused weight out of the sum would cap a technologies-only
    search at 0.55 and make every result look mediocre against a scale the user
    never chose.
    """
    active: dict[str, float] = {"semantic": weights.semantic}
    values: dict[str, float] = {"semantic": similarity}

    if query.languages:
        active["languages"] = weights.languages
        values["languages"] = language_ratio
    if query.certifications:
        active["certifications"] = weights.certifications
        values["certifications"] = certification_ratio
    if query.education_min is not None:
        active["education"] = weights.education
        values["education"] = 1.0 if meets_education else 0.0

    total = sum(active.values()) or 1.0
    score = sum(values[key] * weight / total for key, weight in active.items())
    return score, {key: weight / total for key, weight in active.items()}
