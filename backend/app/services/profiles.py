"""Who a CV is about.

A shortlist that reads ``17111768.pdf`` is a shortlist nobody can discuss. What
a bid manager needs on the row is the person — or, failing that, what the
person does.

Both cases occur in the corpus and they are genuinely different:

* an Inetum CV opens with a letterhead, then a name, then a job title:
  ``… Saint-Ouen-Sur-Seine - France 1 / 3 RAMI OUALI Consultant Intégration
  FORMATIONS …``
* a public-dataset resume is **anonymised** and opens with the job title
  itself: ``INFORMATION TECHNOLOGY TECHNICIAN I Summary Versatile …``

So there is not always a name, and pretending otherwise would invent one. The
label falls back down a ladder — name, then headline, then filename — and
records which rung it landed on, because "we could not find a name" and "this
person is called X" must not look the same downstream.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["ProfileIdentity", "extract_identity"]

#: Section headings that end the header block. Everything a CV says about who
#: it belongs to sits before the first of these.
_SECTIONS = (
    "formations", "diplomes", "diplômes", "summary", "professional summary",
    "career overview", "qualifications", "executive profile", "profile",
    "experience", "experiences", "competences", "compétences", "skills",
    "education", "objective", "highlights", "certifications", "resume",
    "profil", "parcours", "expertise", "accomplishments",
)

#: Letterhead noise: the publisher's own address and page furniture, which sit
#: *before* the name and would otherwise be read as one.
_LETTERHEAD = re.compile(
    r"(inetum\.com|stories|\d+\s*/\s*\d+|\d{5}\s|www\.|https?://|"
    r"rue\s|avenue\s|boulevard\s|france\b|tel\.?\s|\+\d{6,})",
    re.IGNORECASE,
)

#: Occupational vocabulary. A run of capitals containing one of these is a job
#: title, not a person — which is the whole difficulty, since "RAMI OUALI" and
#: "INFORMATION TECHNOLOGY MANAGER" are both simply capitals.
_OCCUPATIONS = (
    "manager", "technician", "engineer", "accountant", "consultant", "developer",
    "analyst", "director", "architect", "specialist", "administrator", "designer",
    "officer", "assistant", "coordinator", "supervisor", "lead", "head", "chef",
    "advocate", "teacher", "trainer", "expert", "advisor", "auditor", "banker",
    "information technology", "it ", "hr ", "sales", "finance", "marketing",
    "operations", "business", "project", "product", "data", "cloud", "devops",
    "software", "systems", "network", "security", "quality", "digital",
    "ingenieur", "ingénieur", "responsable", "directeur", "chargé", "charge de",
    "technicien", "développeur", "developpeur", "architecte",
    # Title *modifiers*. Without them "SKE Enterprise Architect" reads as a
    # two-word name followed by "Architect", when SKE is the initialism and
    # "Enterprise Architect" is the whole title.
    "enterprise", "senior", "junior", "principal", "staff", "technical",
    "functional", "solution", "application", "native", "full stack", "fullstack",
)

#: An anonymised Inetum CV identifies its subject by initials — WHT, SKE. Two
#: to four capitals standing alone before a job title is a person, not a word.
_INITIALS = re.compile(r"^[A-Z]{2,4}$")

#: A plausible person's name: two to four capitalised words, no digits.
_NAME = re.compile(r"^[A-ZÀ-ÖØ-Þ][\w'’\-]+(?:\s+[A-ZÀ-ÖØ-Þ][\w'’\-]+){1,3}$")


@dataclass(slots=True)
class ProfileIdentity:
    """What could be established about the person behind a CV."""

    #: ``None`` when the document is anonymised — never a guess.
    name: str | None
    #: The job title, which anonymised resumes lead with and named ones follow
    #: the name with. Useful on its own: "ACCOUNTANT" tells a bid manager more
    #: than a filename ever will.
    headline: str | None
    #: ``name`` | ``headline`` | ``filename`` — which rung of the ladder the
    #: label came from. Stored so a screen can style a real name differently
    #: from a fallback, and so a later pass can target only the weak ones.
    source: str

    @property
    def label(self) -> str:
        return self.name or self.headline or ""


def _fold(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower()


def _looks_occupational(candidate: str) -> bool:
    folded = _fold(candidate)
    return any(word in folded for word in _OCCUPATIONS)


def _header_block(text: str) -> tuple[str, bool]:
    """The text before the first section heading, and whether a letterhead sat
    in front of it."""
    window = text[:1200]
    folded = _fold(window)
    cut = len(window)
    for section in _SECTIONS:
        position = folded.find(_fold(section))
        # Ignore a heading at the very start — some resumes open with the word
        # "Profile" as a banner. Three characters, not twelve: "ACCOUNTANT
        # Summary" puts the real heading at position 11, and a wider guard
        # swallowed the whole summary into the headline.
        if 3 < position < cut:
            cut = position
    head = window[:cut]

    lines = [line.strip() for line in re.split(r"[\n\r]+", head) if line.strip()]
    keep = [line for line in lines if not _LETTERHEAD.search(line)]
    return " ".join(keep).strip(), len(keep) < len(lines)


def extract_identity(text: str | None, *, filename: str = "") -> ProfileIdentity:
    """Read a display identity out of a CV.

    Deliberately conservative. An invented name on a shortlist is worse than a
    job title, because a job title is visibly a description while a name is
    taken as fact.
    """
    if not text or not text.strip():
        return ProfileIdentity(name=None, headline=None, source="filename")

    header, had_letterhead = _header_block(text)
    if not header:
        return ProfileIdentity(name=None, headline=None, source="filename")

    words = header.split()
    name: str | None = None

    # A name is only sought behind a letterhead, and that structural rule does
    # more work than any word list.
    #
    # Measured on 344 CVs: without it, the vocabulary check called
    # "CUSTOMER SERVICE REPRESENTATIVE", "DANCE EDUCATOR" and "FOOD SERVER"
    # names, because no list of occupations covers every job title in the
    # world. But a corporate CV puts its subject *after* a letterhead, while an
    # anonymised resume opens on the job title with no letterhead at all — so
    # the presence of one is the signal that a name is even possible here.
    #
    # It fails safe: a CV with a name and no letterhead is labelled by its job
    # title, which is a worse label rather than a wrong one. Telling a real
    # name from a job title is a judgement about the world, and that is what an
    # LLM pass would do properly.
    if not had_letterhead:
        headline = " ".join(words[:8]).strip(" -–—·|,") or None
        return ProfileIdentity(name=None, headline=headline, source="headline")

    # Where the job title starts. Everything before it is the person; taking
    # "words before the first occupational term" is what separates
    # "RAMI OUALI | Consultant" from "SKE | Enterprise Architect" without
    # needing to know which is a name and which an initialism.
    boundary = len(words)
    for index, word in enumerate(words[:5]):
        if _looks_occupational(word):
            boundary = index
            break

    if boundary == 1 and _INITIALS.match(words[0]):
        name = words[0]
    elif 2 <= boundary <= 4:
        candidate = " ".join(words[:boundary])
        if _NAME.match(candidate) and not _looks_occupational(candidate):
            name = candidate

    remainder = header[len(name) :].strip() if name else header
    headline = " ".join(remainder.split()[:8]).strip(" -–—·|,") or None

    if name:
        return ProfileIdentity(name=name, headline=headline, source="name")
    if headline:
        return ProfileIdentity(name=None, headline=headline, source="headline")
    return ProfileIdentity(name=None, headline=None, source="filename")
