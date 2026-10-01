"""The dated career timeline: what a funder's CV form actually asks for.

``cv_criteria`` answers "what does this person know". This answers "what did
they do, where, and when" — the skeleton every BEI, BAD or bureau d'études CV
form is built around, and the one thing the platform could not fill.

Three measurements over the 462 CVs in the base shaped every rule here.

**The corpus is English-formatted.** ``EDUCATION`` appears as a heading in 364
CVs and ``EXPERIENCE`` in 318, against 4 for ``FORMATION`` and 1 for
``EXPÉRIENCE``. Patterns are therefore English-first with French accepted, not
the reverse — which is the opposite of what the rest of the platform assumes,
and would have been the wrong guess.

**Extraction is line-fragmented.** A single entry arrives as
``Company Name`` / ``December 2009`` / ``to`` / ``Current`` / ``Staff
Accountant`` / ``City`` / ``,`` / ``State`` — one token per line. No rule that
expects a date and its label on the same line survives contact with this. So
the **date range is the anchor**: it is the most reliably detectable thing in a
CV, and the fields are read from the lines around it.

**459 of 462 CVs are anonymised.** Employers read literally ``Company Name``
and places ``City, State``. Those are placeholders from a public resume
dataset, not employers, and writing them into a generated CV would put a
fabricated-looking fact in a contractual document. They are recognised and
dropped, and the entry says so.

The standing rule of this codebase applies with more force here than anywhere
else: a pattern that fires too readily does not raise an error, it invents a
plausible wrong date in a document someone signs.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "CvStructure",
    "Education",
    "Experience",
    "extract_structure",
]

#: What fits the mission box of a funder's form. The first real generation
#: used 1200 and produced a wall of text nobody would read in a CV form.
_MISSION_CHARS = 600

#: A CV yielding more than this many roles is a parsing failure, not a career.
#: Measured: the deepest genuine CV in the base lists eleven.
_MAX_ENTRIES = 25

#: Placeholders from the public resume dataset the base is largely made of.
#: Emitting them as facts would be worse than emitting nothing: a reader cannot
#: tell "we could not read the employer" from "the employer is called Company
#: Name", and only one of those is true.
_ANONYMISED = re.compile(
    r"^\s*(?:company\s+name|city\s*,?\s*state|city|state|employer\s+name|"
    r"\[?\s*company\s*\]?|n/?a)\s*[,.]?\s*$",
    re.IGNORECASE,
)

_MONTHS = (
    r"jan(?:uary|v(?:ier)?)?|feb(?:ruary)?|f[ée]v(?:rier)?|mar(?:ch|s)?|"
    r"apr(?:il)?|avr(?:il)?|may|mai|jun(?:e)?|juin|jul(?:y)?|juil(?:let)?|"
    r"aug(?:ust)?|ao[uû]t?|sep(?:t(?:ember|embre)?)?|oct(?:ober|obre)?|"
    r"nov(?:ember|embre)?|d[ée]c(?:ember|embre)?"
)

_MONTH_NUMBER = {
    "jan": 1, "feb": 2, "fev": 2, "fév": 2, "mar": 3, "apr": 4, "avr": 4,
    "may": 5, "mai": 5, "jun": 6, "jui": 6, "juin": 6, "jul": 7, "aug": 8,
    "aou": 8, "aoû": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12, "déc": 12,
}

#: A line carrying nothing but a date. Never an employer or a job title, and
#: reading one as either produced `employeur=1983` on a real CV.
_DATE_ONLY = re.compile(
    rf"^\s*(?:(?:{_MONTHS})\.?\s*)?(?:\d{{1,2}}\s*/\s*)?(?:19|20)?\d{{2}}\s*$",
    re.IGNORECASE,
)

#: One end of a range: "December 2009", "12/2009", "2009".
_POINT = rf"(?:(?:{_MONTHS})\.?\s+)?(?:\d{{1,2}}\s*/\s*)?(?:19|20)\d{{2}}"
_ONGOING = r"current|present|today|now|aujourd'hui|à\s+ce\s+jour|en\s+cours|présent|present"

#: A range, tolerating the newlines the extractor scatters through it.
#: Anchored on a boundary so "2000" inside "in 2000-2001 revenue" is not read
#: as a role — the surrounding-line rules below are what actually reject it,
#: but the boundary removes the cheapest false positives first.
_RANGE = re.compile(
    rf"\b(?P<start>{_POINT})\s*(?:\n|\s)*(?:-|–|—|to|until|jusqu'?[aà]|au|à)\s*(?:\n|\s)*"
    rf"(?P<end>{_POINT}|{_ONGOING})\b",
    re.IGNORECASE,
)

#: Section headings, English-first because the corpus is.
_EXPERIENCE_HEADING = re.compile(
    r"^\s*(?:work\s+|professional\s+|relevant\s+)?experien[cs]e[s]?\s*:?\s*$"
    r"|^\s*exp[ée]riences?\s+(?:professionnelles?|profesionnelles?)\s*:?\s*$"
    r"|^\s*exp[ée]riences?\s*:?\s*$"
    r"|^\s*employment(?:\s+history)?\s*:?\s*$"
    r"|^\s*parcours\s+professionnel\s*:?\s*$",
    re.IGNORECASE,
)

_EDUCATION_HEADING = re.compile(
    r"^\s*(?:education|academic(?:\s+background)?|qualifications?)\s*(?:and\s+training)?\s*:?\s*$"
    r"|^\s*formations?\s*(?:acad[ée]miques?)?\s*:?\s*$"
    r"|^\s*dipl[oô]mes?\s*:?\s*$"
    r"|^\s*[ée]tudes\s*:?\s*$",
    re.IGNORECASE,
)

#: Headings that end a section. Anything that looks like a standalone heading
#: and is not the one we are reading closes it.
_ANY_HEADING = re.compile(
    r"^\s*(?:skills?|highlights?|summary|profile|certifications?|languages?|"
    r"interests?|references?|accomplishments?|awards?|publications?|projects?|"
    r"comp[ée]tences?|langues?|centres?\s+d'int[ée]r[êe]t|r[ée]f[ée]rences?)\s*:?\s*$",
    re.IGNORECASE,
)

#: Role nouns. A curated lexicon rather than a positional rule, because the
#: position is not stable: measured on real CVs, the job title sits *above* the
#: dates as often as below, and assuming either produced
#: `employeur="Database Programmer/Analyst"` — the title filed as the employer.
#:
#: Deliberately a short list of unambiguous head nouns. "Lead" and "Head" are
#: absent: they appear far more often inside a mission sentence ("lead the
#: migration") than as a title on their own line.
_ROLE_WORDS = re.compile(
    r"\b(?:developer|developpeur|d[ée]veloppeur|engineer|ing[ée]nieur|analyst|analyste|"
    r"manager|responsable|consultant|consultante|architect|architecte|designer|"
    r"technician|technicien|administrator|administrateur|accountant|comptable|"
    r"coordinator|coordinateur|specialist|sp[ée]cialiste|officer|assistant|assistante|"
    r"director|directeur|directrice|programmer|programmeur|scientist|"
    r"chef\s+de\s+projet|charg[ée]\s+de|intern|stagiaire|technicien(?:ne)?|"
    r"supervisor|superviseur|auditor|auditeur|teacher|enseignant|professor|"
    r"nurse|infirmi[eè]re?|advisor|conseill[eè]re?)\b",
    re.IGNORECASE,
)

#: Degree tokens that are only safe *inside* an Education heading.
#:
#: `cv_criteria` is used over whole documents and cannot accept these: "M.S"
#: would fire on "MS Office" and "MS SQL Server", and a bare "Associates" on
#: any sentence about colleagues. Under an Education heading the context is
#: given, and the same tokens become unambiguous.
_SECTION_DEGREE = re.compile(
    r"^\s*(?:associate'?s?|m\.\s?s\.?|b\.\s?s\.?|m\.\s?a\.?|b\.\s?a\.?|"
    r"b\.?\s?tech|m\.?\s?tech|b\.?e\.?|m\.?e\.?)\b",
    re.IGNORECASE,
)

#: A line that is only punctuation — the fragmentation debris.
#:
#: Deliberately does NOT drop "to", "au" or "à". The extractor splits a range
#: across three lines ("December 2009" / "to" / "Current"), so the joiner sits
#: alone on its own line and is exactly what `_RANGE` needs to recognise the
#: pair. Filtering it as noise removed the connector and the match with it:
#: measured, experience recall fell from 79% to 11%.
_NOISE = re.compile(r"^\s*[,;:./|·•\-–—]+\s*$")


@dataclass(slots=True)
class Experience:
    """One role. ``employer`` is ``None`` when the CV anonymised it."""

    debut: str
    fin: str
    poste: str | None
    employeur: str | None
    lieu: str | None
    missions: str
    #: Sort key: (year, month), most recent first. Not serialised.
    _order: tuple[int, int] = field(default=(0, 0), repr=False)


@dataclass(slots=True)
class Education:
    annee: str
    diplome: str
    etablissement: str | None
    _order: int = field(default=0, repr=False)


@dataclass(slots=True)
class CvStructure:
    experiences: list[Experience]
    formations: list[Education]
    #: Why the result looks the way it does — shown in the interface rather
    #: than left for someone to infer from an empty section.
    #: ``ok`` · ``no_text`` · ``no_sections`` · ``anonymised``
    status: str
    #: How many entries lost their employer to the dataset's anonymisation.
    anonymised_fields: int = 0

    def to_dict(self) -> dict[str, Any]:
        def clean(record: Any) -> dict[str, Any]:
            return {k: v for k, v in asdict(record).items() if not k.startswith("_")}

        return {
            "experiences": [clean(item) for item in self.experiences],
            "formations": [clean(item) for item in self.formations],
            "status": self.status,
            "anonymised_fields": self.anonymised_fields,
        }


# ---------------------------------------------------------------------------
def extract_structure(text: str | None) -> CvStructure:
    """Read a dated timeline out of a CV."""
    if not text or not text.strip():
        return CvStructure(experiences=[], formations=[], status="no_text")

    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not _NOISE.match(line)]
    if not lines:
        return CvStructure(experiences=[], formations=[], status="no_text")

    experience_lines = _section(lines, _EXPERIENCE_HEADING)
    education_lines = _section(lines, _EDUCATION_HEADING)

    # No heading found is not a reason to give up on experience: a date range
    # with a job title beside it is recognisable anywhere. It *is* a reason to
    # give up on education, where the only thing distinguishing a degree year
    # from any other year is the section it sits in.
    experiences, anonymised = _read_experiences(experience_lines or lines)
    formations = _read_education(education_lines) if education_lines else []

    status = "ok"
    if not experiences and not formations:
        status = "no_sections"
    elif anonymised and all(item.employeur is None for item in experiences):
        status = "anonymised"

    logger.info(
        "cv_structure.extracted",
        experiences=len(experiences),
        formations=len(formations),
        anonymised_fields=anonymised,
        status=status,
    )
    return CvStructure(
        experiences=experiences,
        formations=formations,
        status=status,
        anonymised_fields=anonymised,
    )


def _section(lines: list[str], heading: re.Pattern[str]) -> list[str]:
    """The lines under one heading, up to the next heading."""
    for index, line in enumerate(lines):
        if heading.match(line):
            collected: list[str] = []
            for candidate in lines[index + 1 :]:
                if heading.match(candidate) or _ANY_HEADING.match(candidate):
                    break
                collected.append(candidate)
            return collected
    return []


def _read_experiences(lines: list[str]) -> tuple[list[Experience], int]:
    """Anchor on date ranges; read the fields from the lines around each one."""
    blob = "\n".join(lines)
    offsets = _line_offsets(lines)
    matches = list(_RANGE.finditer(blob))
    if not matches:
        return [], 0

    entries: list[Experience] = []
    anonymised = 0

    for position, match in enumerate(matches):
        start_line = _line_of(offsets, match.start())
        end_line = _line_of(offsets, match.end() - 1)

        # The employer sits above the dates in every layout observed; the title
        # below. Neither is guaranteed, and an entry with neither is discarded
        # rather than emitted as a bare date: a row saying only "2009-2012" in
        # a funder's form is worse than a row that is not there.
        above, above_hidden = _field(lines, start_line - 1, backwards=True)
        below, below_hidden = _field(lines, end_line + 1, backwards=False)
        anonymised += above_hidden + below_hidden

        title, employer = _assign(above, below)
        location = _location(lines, end_line + 2)

        # Everything until the next anchor is what they actually did.
        next_line = (
            _line_of(offsets, matches[position + 1].start())
            if position + 1 < len(matches)
            else len(lines)
        )
        missions = _missions(lines, end_line + 1, next_line, skip={title, location})

        if not title and not employer and not missions:
            continue

        entries.append(
            Experience(
                debut=_clean(match.group("start")),
                fin=_clean(match.group("end")),
                poste=title,
                employeur=employer,
                lieu=location,
                missions=missions,
                _order=_sort_key(match.group("start")),
            )
        )

    if len(entries) > _MAX_ENTRIES:
        # Reading fifty roles out of one CV means the anchors caught something
        # that is not a career — a table of figures, a list of years. Returning
        # the longest plausible prefix would be inventing a boundary; returning
        # nothing says plainly that this document was not understood.
        logger.warning("cv_structure.too_many_entries", found=len(entries))
        return [], anonymised

    entries.sort(key=lambda item: item._order, reverse=True)
    return entries, anonymised


def _read_education(lines: list[str]) -> list[Education]:
    """Degrees, with the year and the school when they are stated.

    Only inside an ``Education`` heading. Outside it, nothing distinguishes a
    graduation year from a project date, and guessing wrong writes a false
    diploma date into a submitted CV.
    """
    from app.services.cv_criteria import extract_criteria

    entries: list[Education] = []
    year_pattern = re.compile(r"\b(19|20)\d{2}\b")

    for index, line in enumerate(lines):
        # A line only counts as a degree when the criteria extractor — already
        # hardened against "Master Service Agreement" and "ingénieur système" —
        # recognises one in it.
        if extract_criteria(line).education_level is None and not _SECTION_DEGREE.match(line):
            continue

        year = year_pattern.search(line)
        if not year:
            # Look just below: "Master of Science" / "2014" is the fragmented
            # layout's usual shape.
            for candidate in lines[index + 1 : index + 4]:
                year = year_pattern.search(candidate)
                if year:
                    break

        school = None
        for candidate in lines[index + 1 : index + 4]:
            if _ANONYMISED.match(candidate) or year_pattern.fullmatch(candidate.strip()):
                continue
            if (
                extract_criteria(candidate).education_level is not None
                or _SECTION_DEGREE.match(candidate)
            ):
                break
            if 3 < len(candidate) < 120:
                school = candidate
                break

        entries.append(
            Education(
                annee=year.group(0) if year else "",
                diplome=_clean(line)[:200],
                etablissement=school,
                _order=int(year.group(0)) if year else 0,
            )
        )

    if len(entries) > _MAX_ENTRIES:
        logger.warning("cv_structure.too_many_degrees", found=len(entries))
        return []

    entries.sort(key=lambda item: item._order, reverse=True)
    return entries


# ---------------------------------------------------------------------------
def _field(lines: list[str], index: int, *, backwards: bool) -> tuple[str | None, int]:
    """One label beside a date anchor, or nothing.

    Returns ``(value, anonymised_count)``. A recognised placeholder counts as
    anonymised and yields ``None``: a blank the reader can question beats a
    fabricated-looking employer they cannot.
    """
    step = -1 if backwards else 1
    for offset in range(3):
        position = index + offset * step
        if not 0 <= position < len(lines):
            return None, 0
        candidate = lines[position]
        if _ANONYMISED.match(candidate):
            return None, 1
        if _RANGE.search(candidate) or _ANY_HEADING.match(candidate):
            return None, 0
        if _DATE_ONLY.match(candidate):
            continue
        # A sentence is a mission line, not a job title.
        if 2 < len(candidate) <= 90 and not candidate.endswith("."):
            return _clean(candidate), 0
    return None, 0


def _assign(above: str | None, below: str | None) -> tuple[str | None, str | None]:
    """Decide which neighbour of the dates is the job title.

    A role noun decides it when one side has one and the other does not. When
    neither does — or both do — the fallback is positional, and it favours the
    line *above*, because that is what the corpus shows more often once the
    anonymised employer line has been removed.

    Whatever is left over is offered as the employer, and on this base that is
    almost always ``None``: 459 of 462 CVs replaced their employers with the
    literal string "Company Name".

    The positional fallback is kept but **shape-checked**, and the numbers are
    why. Reading a produced expert sheet showed roles titled "Identifies and
    solves" and "information while evaluating potential sources of" — mission
    fragments printed as job titles on a page meant for a buyer. Measured over
    1 870 entries, removing the fallback entirely would have cost 464 titles to
    delete 29 bad ones: fifteen good for one bad. So the fallback stays, and
    only candidates that do not look like a title are dropped.
    """
    above_is_role = bool(above and _ROLE_WORDS.search(above))
    below_is_role = bool(below and _ROLE_WORDS.search(below))

    if above_is_role and not below_is_role:
        return above, _employer(below)
    if below_is_role and not above_is_role:
        return below, _employer(above)
    # Neither neighbour names a role: position decides, but only for a
    # candidate shaped like a title.
    if above and _looks_like_title(above):
        return above, _employer(below)
    if below and _looks_like_title(below):
        return below, _employer(above)
    # Rather a role with no title than a sentence fragment presented as one.
    # The dates and the missions still carry the entry.
    return None, _employer(above or below)


def _looks_like_title(value: str) -> bool:
    """Whether a line could be a job title rather than a piece of a sentence.

    Applied *only* to the positional fallback — a candidate the role lexicon
    already recognised is never second-guessed, so a lowercase "chef de projet"
    still passes through the branch above.

    Two markers, both measured on the real fragments: they start lowercase
    ("information while evaluating…", "and management, data privacy") or end on
    a word no title ends on ("… and", "… and/or", "… of").
    """
    text = value.strip()
    if not text or not text[0].isupper():
        return False
    if len(text.split()) > 8:
        return False
    return not _DANGLING.search(text)


#: Words a job title does not end on. A line that does is the middle of a
#: sentence, whatever else it looks like.
_DANGLING = re.compile(
    r"\b(?:and(?:/or)?|or|of|for|with|to|in|the|a|et|ou|de|des|du|la|le|les|pour|"
    r"avec|dans)\s*[,;]?\s*$",
    re.IGNORECASE,
)


def _employer(value: str | None) -> str | None:
    """Keep a leftover label only if it could be a company name.

    Observed on a real generated CV: the first line of a mission block was
    filed as the employer and printed as one. A company name is short and has
    few words; a mission sentence is neither. A blank is a hole a reader can
    question, where a sentence posing as an employer is a wrong fact they
    cannot.

    The shape check is the same one the title slot uses, and it is here for a
    reason learned the hard way: tightening only the title slot moved the
    fragment rather than removing it. "information while evaluating potential
    sources of" stopped being printed as a job title and started being printed
    as a company. Both fields land on a page a buyer reads, so both answer to
    the same rule.
    """
    if not value:
        return None
    if len(value) > 60 or len(value.split()) > 6:
        return None
    return value if _looks_like_title(value) else None


def _location(lines: list[str], index: int) -> str | None:
    if not 0 <= index < len(lines):
        return None
    candidate = lines[index]
    if _ANONYMISED.match(candidate) or len(candidate) > 60:
        return None
    return _clean(candidate) if "," in candidate or len(candidate.split()) <= 3 else None


def _missions(lines: list[str], start: int, stop: int, *, skip: set[str | None]) -> str:
    """What the person did, as written, capped.

    Capped because a funder's form has a box, not a chapter — and because an
    unbounded copy of a CV section into a generated document is how a
    twelve-page CV happens.
    """
    collected: list[str] = []
    for line in lines[max(start, 0) : min(stop, len(lines))]:
        # A heading ends the role, whatever the caller thought the boundary
        # was. Without this, a CV with no "Experience" heading anchors on the
        # whole document and the last role absorbs the entire Education
        # section — observed on a real generated CV, where a degree list
        # appeared inside a job description.
        if _ANY_HEADING.match(line) or _EDUCATION_HEADING.match(line):
            break
        cleaned = _clean(line)
        if not cleaned or cleaned in skip or _ANONYMISED.match(cleaned):
            continue
        if len(cleaned) < 12:
            continue
        collected.append(cleaned)
        if sum(len(item) for item in collected) > _MISSION_CHARS:
            break
    return " ".join(collected)[:_MISSION_CHARS]


def _line_offsets(lines: list[str]) -> list[int]:
    offsets, running = [], 0
    for line in lines:
        offsets.append(running)
        running += len(line) + 1  # the "\n" the blob was joined with
    return offsets


def _line_of(offsets: list[int], position: int) -> int:
    return max(0, bisect_right(offsets, position) - 1)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" ,;:·•-–—")


def _sort_key(point: str) -> tuple[int, int]:
    year = re.search(r"(19|20)\d{2}", point)
    month = 0
    name = re.match(r"\s*([A-Za-zÀ-ÿ]{3,})", point)
    if name:
        month = _MONTH_NUMBER.get(name.group(1)[:3].lower(), 0)
    else:
        numeric = re.match(r"\s*(\d{1,2})\s*/", point)
        if numeric:
            month = int(numeric.group(1))
    return (int(year.group(0)) if year else 0, month)
