"""Ranking candidates against a tender.

Built on a measurement rather than on faith in cosine similarity. Searching
this corpus with the embedding model alone gave, on real data:

    "sécurisation des échanges, authentification unique et jetons"  →  0.315
    "comptabilité générale et consolidation des bilans"             →  0.270

A true requirement and a deliberately unrelated one, forty-five thousandths
apart. No threshold separates that. Worse, the top hit for the security query
was the CV's letterhead — an address block that states no skill at all, yet
sits at middling distance from everything because it means nothing in
particular.

So similarity is one signal among three, and on its own it decides nothing:

**The lock.** Technologies the tender names are checked literally, on word
boundaries. A candidate who has never written OAuth cannot be the best OAuth
match however the vectors fall. This is the anti-bias rule from the
specification: a well-written CV without the required skills collapses rather
than charming the ranking.

**Coverage.** A tender states many requirements. A candidate matching one of
them perfectly and none of the others is worse than one covering eight
decently, and a single mean similarity hides exactly that difference.

**Evidence.** Every score carries the passages and terms that produced it. A
ranking a bid manager cannot audit is a ranking they are right to distrust.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.core.identity import normalize_text
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "CandidateMatch",
    "MatchWeights",
    "Requirement",
    "extract_requirements",
    "match_tender",
    "required_technologies",
    "technology_lexicon",
]

#: Sentences that announce an obligation. A dossier is mostly context; these
#: mark the parts that constrain who may bid, which is what a CV answers.
_REQUIREMENT_MARKERS = (
    "doit", "devra", "exige", "requis", "obligatoire", "necessaire",
    "competence", "profil", "experience", "qualification", "maitrise",
    "certification", "expertise", "intervenant", "consultant", "ingenieur",
    "savoir-faire", "connaissance",
)

#: A passage carrying none of these is prose we cannot match a person against.
_MIN_REQUIREMENT_CHARS = 120

#: Contact blocks, page furniture and legal footers. They state no skill, but
#: they still produce a vector, and a vector that means nothing in particular
#: sits at middling distance from everything — which is how a letterhead came
#: top of a security search.
_BOILERPLATE = re.compile(
    r"^\s*(page\s+\d+|\d+\s*/\s*\d+|www\.|https?://|tel\.|siret|rcs\b|"
    r"\d{5}\s+[A-Z])",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Requirement:
    """One passage of a tender that a candidate can be measured against."""

    text: str
    document: str | None
    position: int
    #: Technologies named in this passage, if any. Drives the lock.
    technologies: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MatchWeights:
    """How the three signals combine.

    Versioned with the score so a past ranking stays explainable after these
    change — the same rule the tender scoring profile follows.
    """

    version: str = "match-v1"
    #: How well the matched passages fit, on average.
    similarity: float = 0.45
    #: How many of the tender's requirements the candidate speaks to at all.
    coverage: float = 0.35
    #: Share of the named technologies the candidate actually evidences.
    technologies: float = 0.20

    def as_dict(self) -> dict[str, float]:
        return {
            "similarity": self.similarity,
            "coverage": self.coverage,
            "technologies": self.technologies,
        }


@dataclass(slots=True)
class CandidateMatch:
    cv_id: str
    filename: str
    score: float
    similarity: float
    coverage: float
    technology_ratio: float
    matched_technologies: list[str]
    missing_technologies: list[str]
    #: (requirement position, cosine, passage) — the proof, not a summary of it.
    evidence: list[tuple[int, float, str]]
    vetoed: bool = False
    veto_reason: str | None = None
    #: Who the profile is about, when the CV says so. Falls back to the job
    #: title, then to the filename — a shortlist of opaque identifiers cannot
    #: be discussed in a meeting.
    display_name: str | None = None
    headline: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cv_id": self.cv_id,
            "filename": self.filename,
            "display_name": self.display_name,
            "headline": self.headline,
            "label": self.display_name or self.headline or self.filename,
            "score": round(self.score, 4),
            "similarity": round(self.similarity, 4),
            "coverage": round(self.coverage, 4),
            "technology_ratio": round(self.technology_ratio, 4),
            "matched_technologies": self.matched_technologies,
            "missing_technologies": self.missing_technologies,
            "vetoed": self.vetoed,
            "veto_reason": self.veto_reason,
            "evidence": [
                {"requirement": position, "score": round(score, 4), "passage": passage[:400]}
                for position, score, passage in self.evidence
            ],
        }


def _is_boilerplate(text: str) -> bool:
    """Whether a passage is page furniture rather than content.

    Measured on a real CV: the letterhead — "inetum.com Stories, 5-7 rue Touzet
    Gaillard, 93400 Saint-Ouen" — came top of a search for authentication and
    tokens, ahead of the passage that literally lists SSO, SAML and OAuth. A
    block naming no skill should never be evidence for one.
    """
    stripped = text.strip()
    if len(stripped) < 60:
        return True
    if _BOILERPLATE.match(stripped):
        return True
    # Mostly digits and punctuation: an address, a table of dates, a reference
    # block. Letters are what carry a competence.
    letters = sum(1 for ch in stripped if ch.isalpha())
    return letters / max(len(stripped), 1) < 0.55


@lru_cache(maxsize=1)
def technology_lexicon() -> tuple[str, ...]:
    """Every technology the platform knows how to recognise.

    Loaded from ``config/technologies.yaml`` — deliberately *not* from
    ``scoring.yaml``. That file answers "is this tender in our line of
    business" and holds service categories: tierce maintenance applicative,
    portail, assistance technique. Locked against it, a consultation demanding
    Symfony, PHP, Docker, Kubernetes, GitLab and SonarQube matched only
    "monitoring" and "DevOps", and every candidate scored 10-20% on
    technologies that had nothing to do with the ones asked for.
    """
    from app.core.config import load_yaml_config

    try:
        document = load_yaml_config("technologies")
    except Exception:
        logger.warning("matching.lexicon_unavailable")
        return ()
    terms: list[str] = []
    for group in (document.get("groups") or {}).values():
        for term in group or []:
            if term and term not in terms:
                terms.append(str(term))
    return tuple(terms)


#: Terms that are also ordinary words, with the contexts that disqualify an
#: occurrence. Word boundaries are not enough for these: "Java" matched
#: "Central **Java** Inter-Mission School" — the Indonesian island — and put an
#: English teacher on a Java shortlist. The same trap as "SAGE de la Lys" in
#: scoring, and the same remedy: for an ambiguous token, demand that it not be
#: standing in a context that gives it another meaning.
#:
#: An occurrence in one of these contexts is discarded; a term surviving
#: nowhere else in the document is absent. A CV that mentions both the island
#: and the language therefore still counts, which is the behaviour that keeps
#: this from costing recall.
_AMBIGUOUS: dict[str, str] = {
    "java": (
        r"(?:central|west|east|jawa|ile de|island|sea of|mer de)\s+java"
        r"|java\s+(?:sea|island|timur|barat|tengah)"
    ),
    "tableau": (
        r"\btableau\s+(?:de\s+bord|comparatif|r[ée]capitulatif|suivant|ci-)"
        r"|\bdans\s+le\s+tableau\b"
    ),
    "rest": r"\bthe\s+rest\b|\brest\s+of\s+the\b|\bau\s+repos\b",
    "safe": (
        r"\bsafe\s+(?:working|environment|practices?|manner|distance)\b"
        r"|\bkeep\s+safe\b"
    ),
}


def required_technologies(text: str) -> list[str]:
    """Technologies the text names, in the order the lexicon lists them.

    Word-boundary matching, for the reason scoring learned the hard way:
    substring matching fired ``SI`` inside "assimilés" and ``.NET`` inside a
    buyer named "Khazanet", and put a waste-collection contract at the top of
    the dashboard.

    Boundaries alone are still not enough for a handful of tokens that are also
    ordinary words — see :data:`_AMBIGUOUS`.
    """
    from app.services.scoring import _contains_term

    blob = normalize_text(text)
    found: list[str] = []
    for term in technology_lexicon():
        if not _contains_term(blob, term):
            continue
        disqualifier = _AMBIGUOUS.get(term.lower())
        if disqualifier and not _has_genuine_occurrence(blob, term, disqualifier):
            continue
        found.append(term)
    return found


def _has_genuine_occurrence(blob: str, term: str, disqualifier: str) -> bool:
    """Whether the term appears at least once outside a disqualifying phrase.

    The disqualifier must *overlap* the occurrence, not merely sit near it. A
    first version searched a 40-character window and produced a worse bug than
    the one it fixed: a genuine Java developer who happened to mention growing
    up in Central Java lost the skill entirely, because the island poisoned the
    language two sentences away. A false negative on a real competence costs
    more than a false positive a human can dismiss on sight.
    """
    pattern = _term_pattern_for(term)
    if pattern is None:
        return True
    spans = [m.span() for m in re.finditer(disqualifier, blob, re.IGNORECASE)]
    for match in pattern.finditer(blob):
        inside = any(start <= match.start() and match.end() <= end for start, end in spans)
        if not inside:
            return True
    return False


def _term_pattern_for(term: str) -> re.Pattern[str] | None:
    from app.services.scoring import _term_pattern

    return _term_pattern(term)


def extract_requirements(
    passages: list[tuple[str, str | None, int, int]], *, limit: int = 20
) -> list[Requirement]:
    """Pick the passages a candidate can actually be measured against.

    ``passages`` is ``(text, document, position, priority)`` as stored in the
    index. Most of a dossier is context — payment terms, penalties, the buyer's
    address — and searching every passage would cost one vector query each for
    hundreds of passages while burying the few that constrain who may bid.

    Selection is ordered, not filtered: substantive documents first, then
    passages that speak of obligations, then length. A dossier that names no
    requirement at all still yields its best passages rather than nothing.
    """
    candidates: list[tuple[tuple[int, int, int], Requirement]] = []

    for text, document, position, priority in passages:
        if _is_boilerplate(text):
            continue
        folded = normalize_text(text)
        obligations = sum(1 for marker in _REQUIREMENT_MARKERS if marker in folded)
        if len(text) < _MIN_REQUIREMENT_CHARS:
            continue
        # Lower sorts first: substantive document, then most obligation words,
        # then longest.
        rank = (priority, -obligations, -len(text))
        candidates.append(
            (rank, Requirement(text=text, document=document, position=position))
        )

    candidates.sort(key=lambda item: item[0])
    chosen = [requirement for _, requirement in candidates[:limit]]
    for requirement in chosen:
        requirement.technologies = required_technologies(requirement.text)
    return chosen


#: Below this cosine, a passage is not evidence of anything.
#:
#: Calibrated twice, and the second time is the one that counts. A first value
#: of 0.32 came from comparing short phrases; against real 1 200-character
#: passages every comparison scores far higher, because two pieces of
#: professional prose resemble each other whatever they discuss. Measured over
#: 344 CVs against a real dossier:
#:
#:     best passage per CV, in-domain      median 0.635
#:     best passage per CV, out-of-domain  median 0.603
#:
#: Thirty-two thousandths apart, and the *highest* out-of-domain score (0.752)
#: beat the highest in-domain one (0.715). Chunk-level cosine measures register,
#: not competence. The floor is therefore set where it excludes obvious noise
#: and nothing else — the discrimination has to come from the lock and from
#: coverage, and pretending otherwise by tuning this number would only hide
#: that.
_EVIDENCE_FLOOR = 0.45


def match_tender(
    *,
    tender_text: str,
    requirements: list[Requirement],
    tenant: str,
    weights: MatchWeights | None = None,
    limit: int = 20,
    per_requirement: int = 200,
    required: list[str] | None = None,
    veto_sample: int = 8,
) -> list[CandidateMatch]:
    """Rank the organisation's candidates against one tender.

    One vector search per requirement rather than one for the whole tender.
    Averaging a dossier into a single vector produces a point that means
    "public sector IT project" and ranks every IT consultant identically; asking
    each requirement separately is what lets coverage be measured at all.

    ``per_requirement`` is deliberately generous. At 5 it truncated the pool
    before anything was scored: over 344 candidates only 32 were ever
    considered, and a solid all-rounder placing sixth on every requirement
    never entered the calculation at all. Retrieval is milliseconds; the
    ranking that follows is where selection belongs.
    """
    from app.core.config import get_settings
    from app.services.embeddings import get_embedder
    from app.services.scoring import _contains_term
    from app.services.vectors import get_vector_store

    weights = weights or MatchWeights()
    if not requirements:
        return []

    embedder = get_embedder()
    store = get_vector_store()
    collection = get_settings().vector.cv_collection

    # Technologies wanted by the tender as a whole, not per requirement: a
    # dossier names its stack once and constrains every role by it.
    #
    # ``required`` lets the caller supply a list the lexicon could not produce
    # — an LLM pass reading the requirement passages catches what no curated
    # vocabulary contains. Passed in rather than fetched here so this function
    # stays pure and testable without a network.
    wanted = required if required is not None else required_technologies(tender_text)

    # cv_id -> accumulated evidence
    hits: dict[str, dict[str, Any]] = {}

    vectors = embedder.encode_many([r.text for r in requirements])
    for requirement, vector in zip(requirements, vectors, strict=True):
        found = store.search(
            collection,
            vector,
            tenant=tenant,
            limit=per_requirement,
            min_score=_EVIDENCE_FLOOR,
        )
        # One requirement counts once per candidate, at its best passage.
        # Without this a CV that repeats itself outranks one that says a thing
        # once and means it.
        best_per_cv: dict[str, Any] = {}
        for hit in found:
            owner = str(hit.payload.get("owner_id") or "")
            if not owner:
                continue
            if owner not in best_per_cv or hit.score > best_per_cv[owner].score:
                best_per_cv[owner] = hit

        for owner, hit in best_per_cv.items():
            entry = hits.setdefault(
                owner,
                {"filename": hit.payload.get("filename") or owner, "evidence": []},
            )
            entry["evidence"].append((requirement.position, hit.score, hit.text))

    if not hits:
        return []

    # The lock needs the candidate's full text, not the passages that happened
    # to rank: a technology named once, in a passage no requirement matched,
    # is still a technology the candidate has.
    texts, identities = _cv_texts(list(hits))

    results: list[CandidateMatch] = []
    total_requirements = len(requirements)
    for cv_id, entry in hits.items():
        evidence = sorted(entry["evidence"], key=lambda item: -item[1])
        similarity = sum(score for _, score, _ in evidence) / len(evidence)
        coverage = len(evidence) / total_requirements

        blob = normalize_text(texts.get(cv_id, ""))
        name, headline = identities.get(cv_id, (None, None))
        matched = [term for term in wanted if _contains_term(blob, term)]
        missing = [term for term in wanted if term not in matched]
        ratio = len(matched) / len(wanted) if wanted else 1.0

        score = (
            weights.similarity * similarity
            + weights.coverage * coverage
            + weights.technologies * ratio
        )

        # The veto, scaled to how much the tender asked for.
        #
        # One match was the original rule and it was too generous. A dossier
        # naming twenty-four technologies is satisfied by a single hit, and a
        # single hit out of twenty-four is chance — an accountant who once used
        # SharePoint cleared the bar on a tierce-maintenance tender and ranked
        # third, because similarity and coverage then carried them. The floor
        # therefore grows with the demand: a short stack still needs one match,
        # a long one needs proportionate evidence.
        vetoed = bool(wanted) and len(matched) < _veto_floor(len(wanted))
        results.append(
            CandidateMatch(
                cv_id=cv_id,
                filename=str(entry["filename"]),
                score=0.0 if vetoed else score,
                similarity=similarity,
                coverage=coverage,
                technology_ratio=ratio,
                matched_technologies=matched,
                missing_technologies=missing,
                evidence=evidence[:5],
                display_name=name,
                headline=headline,
                vetoed=vetoed,
                veto_reason=(
                    f"{len(matched)} des {len(wanted)} technologies exigées "
                    f"sont attestées ; il en faut au moins {_veto_floor(len(wanted))}."
                    if vetoed
                    else None
                ),
            )
        )

    results.sort(key=lambda match: (-match.score, match.filename))

    # Returned in two parts rather than as one truncated list.
    #
    # A vetoed candidate scores zero, so it always sorts last — and a top-20
    # slice therefore never contains one. The interface reported "0 écartés"
    # on a run where the veto had floored three hundred profiles, which reads
    # as "the rule did nothing" when it did everything. The shortlist and the
    # evidence that it was filtered are two different answers and both are
    # owed to the reader.
    kept = [match for match in results if not match.vetoed]
    refused = [match for match in results if match.vetoed]
    return kept[:limit] + refused[:veto_sample]


def _veto_floor(required: int) -> int:
    """How many named technologies a candidate must evidence to be considered.

    Deliberately shallow — one for a handful, then roughly one per eight. A
    steeper rule would empty the shortlist on dossiers that list every product
    in their estate, and an empty shortlist teaches a bid manager to stop
    opening the panel.
    """
    if required <= 3:
        return 1
    return max(1, min(3, round(required / 8)))


def _cv_texts(
    cv_ids: list[str],
) -> tuple[dict[str, str], dict[str, tuple[str | None, str | None]]]:
    """Text and display identity for the candidates that surfaced, in one query.

    Fetched together because both are needed for every candidate and a second
    round trip per profile would turn one query into twenty.
    """
    import uuid as uuid_module

    from sqlalchemy import select

    from app.db.models.cv import CV
    from app.db.session import session_scope

    try:
        identifiers = [uuid_module.UUID(value) for value in cv_ids]
    except ValueError:
        return {}, {}

    with session_scope() as session:
        rows = session.execute(
            select(CV.id, CV.extracted_text, CV.display_name, CV.headline).where(
                CV.id.in_(identifiers)
            )
        ).all()
        return (
            {str(row[0]): row[1] or "" for row in rows},
            {str(row[0]): (row[2], row[3]) for row in rows},
        )
