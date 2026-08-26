"""What a CV evidences: technologies, languages, education, certifications.

Read once at import and stored, never recomputed per search. A recruiter
filtering "Java + anglais + Bac+5" over several hundred profiles cannot wait
for several hundred documents to be parsed, and the answer does not change
between two searches anyway.

Everything here is deliberately conservative, for a reason this project has
learned three times now. A pattern that fires too readily does not produce a
visible error; it produces a plausible-looking result that is wrong. ``SAGE``
matched a water-management plan, ``rc`` matched inside "recherche", and
``Master`` matches inside "**Master** Service Agreement" — a contract clause,
not a degree. Each rule below therefore demands its context.

The counterpart matters just as much: **silence is not absence**. Measured over
344 CVs, 18% state a language at all. A candidate who never wrote "anglais" on
their CV very probably speaks it, so what is extracted here is *evidence*, not
truth — which is why the search treats languages, diplomas and certifications
as ranking signals and only technologies as a filter.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

__all__ = ["CvCriteria", "extract_criteria", "normalise_language"]

#: Degree wording mapped to years after the baccalauréat, which is the unit a
#: French recruiter filters on. An exact label is what a CV writes; "Bac+5" is
#: what a job description asks for.
_EDUCATION: tuple[tuple[str, int], ...] = (
    # Longest and most specific first — "Master of Science" must win over any
    # looser rule that might follow.
    (r"\bdoctorat\b|\bph\.?d\b|\bdocteur\s+en\b", 8),
    (r"\bmast[eè]re?\s+(?:of|en|2|ii|spécialisé|specialise|professionnel)\b", 5),
    (r"\bmaster'?s?\s+(?:of|in|degree)\b", 5),
    (r"\bm\.?sc\b|\bmba\b|\bm2\b", 5),
    (r"\bdipl[oô]me\s+(?:national\s+)?d'ing[ée]nieur\b|\bing[ée]nieur\s+d'[ée]tat\b", 5),
    (r"\bbac\s*\+\s*5\b", 5),
    (r"\bbachelor'?s?\s+(?:of|in|degree)\b|\bb\.?sc\b", 3),
    (r"\blicence\s+(?:en|professionnelle|fondamentale)\b|\bbac\s*\+\s*3\b", 3),
    (r"\bbts\b|\bdut\b|\bbac\s*\+\s*2\b|\bassociate'?s?\s+degree\b", 2),
)

#: Bare "ingénieur" is a job title as often as a qualification ("ingénieur
#: système"), so it only counts when the sentence calls it a diploma. Kept
#: apart from the table above to make that distinction explicit rather than
#: buried in a regex.
_ENGINEER_DEGREE = re.compile(
    r"\b(?:dipl[oô]m[ée]?|titre|formation)\s+(?:\w+\s+){0,2}d'ing[ée]nieur\b", re.IGNORECASE
)

_EDUCATION_LABELS = {
    8: "Doctorat",
    5: "Bac+5 (Master / Ingénieur)",
    3: "Bac+3 (Licence / Bachelor)",
    2: "Bac+2 (BTS / DUT)",
}

#: Languages, with the spellings a CV actually uses. The value is the canonical
#: form so "English", "anglais" and "Anglais" become one filterable token.
_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("français", r"\bfran[çc]ais\b|\bfrench\b"),
    ("anglais", r"\banglais\b|\benglish\b"),
    ("arabe", r"\barabe\b|\barabic\b"),
    ("espagnol", r"\bespagnol\b|\bspanish\b"),
    ("allemand", r"\ballemand\b|\bgerman\b"),
    ("italien", r"\bitalien\b|\bitalian\b"),
)

#: Certifications worth filtering on. A curated list rather than "any word near
#: 'certified'": the loose version returned "Certified copy of the diploma" and
#: "certification of employment", which are documents, not qualifications.
_CERTIFICATIONS: tuple[tuple[str, str], ...] = (
    ("AWS Certified", r"\baws\s+certified\b"),
    ("Azure", r"\b(?:az-\d{3}|microsoft\s+certified.{0,20}azure)\b"),
    ("Google Cloud", r"\bgoogle\s+cloud\s+certified\b|\bgcp\s+certified\b"),
    ("PMP", r"\bpmp\b"),
    ("PRINCE2", r"\bprince\s?2\b"),
    ("ITIL", r"\bitil\b"),
    ("TOGAF", r"\btogaf\b"),
    ("Scrum Master", r"\b(?:certified\s+)?scrum\s?master\b|\bcsm\b|\bpsm\s?[i1]{1,3}\b"),
    ("SAFe", r"\bsafe\s?\d?\s+(?:agilist|architect|practitioner)\b"),
    ("CISSP", r"\bcissp\b"),
    ("CISA", r"\bcisa\b"),
    ("CEH", r"\bceh\b|\bcertified\s+ethical\s+hacker\b"),
    ("CCNA", r"\bccna\b"),
    ("CCNP", r"\bccnp\b"),
    ("RHCE", r"\brhce\b|\bred\s+hat\s+certified\b"),
    ("Oracle Certified", r"\boracle\s+certified\b|\bocp\b"),
    ("WSO2", r"\bwso2\s+certified\b"),
    ("TOEIC", r"\btoeic\b"),
    ("TOEFL", r"\btoefl\b"),
)


@dataclass(slots=True)
class CvCriteria:
    """Evidence read from one CV. Absence means "not stated", never "not held"."""

    technologies: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    #: Years after the baccalauréat, the unit a job description asks in.
    education_level: int | None = None
    education_label: str | None = None
    certifications: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "technologies": self.technologies,
            "languages": self.languages,
            "education_level": self.education_level,
            "education_label": self.education_label,
            "certifications": self.certifications,
        }


def _fold(text: str) -> str:
    """Lowercase and strip accents, so "Ingénieur" and "Ingenieur" match once."""
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower()


def normalise_language(value: str) -> str | None:
    """Map a spelling to its canonical token, or ``None`` if unrecognised."""
    folded = _fold(value).strip()
    for canonical, pattern in _LANGUAGES:
        if re.search(pattern, folded, re.IGNORECASE) or folded == _fold(canonical):
            return canonical
    return None


def extract_criteria(text: str | None) -> CvCriteria:
    """Read filterable criteria out of a CV's text."""
    if not text or not text.strip():
        return CvCriteria()

    from app.services.matching import required_technologies

    folded = _fold(text)

    languages = [
        canonical
        for canonical, pattern in _LANGUAGES
        if re.search(pattern, folded, re.IGNORECASE)
    ]

    certifications = [
        label
        for label, pattern in _CERTIFICATIONS
        if re.search(pattern, folded, re.IGNORECASE)
    ]

    # The highest level found, not the first: a CV lists its degrees oldest
    # first as often as newest first, and what a recruiter filters on is the
    # ceiling.
    level: int | None = None
    for pattern, years in _EDUCATION:
        if re.search(pattern, folded, re.IGNORECASE):
            level = max(level or 0, years)
    if level is None and _ENGINEER_DEGREE.search(text):
        level = 5

    return CvCriteria(
        # Reuses the same lexicon the tender lock uses, so "this CV has Java"
        # means exactly what "this tender wants Java" means. Two vocabularies
        # for one comparison is how a filter starts missing its own matches.
        technologies=required_technologies(text),
        languages=languages,
        education_level=level,
        education_label=_EDUCATION_LABELS.get(level) if level else None,
        certifications=certifications,
    )
