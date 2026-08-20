"""Removing what must not leave the server.

``parcours_smarttender.html`` states as a cross-cutting guarantee that CVs never
leave the platform (RGPD / INPDP). Calling a hosted model contradicts that
literally — unless what is sent carries no one's identity. That is what this
module is for: everything crossing the boundary passes through here first.

Three principles, and the order matters.

**Redaction is not deletion.** A stripped span is replaced by a typed
placeholder — ``[NOM]``, ``[EMAIL]`` — not by nothing. The model still sees the
*shape* of the document, so "contacter [NOM] à [EMAIL]" remains a sentence with
a subject. Removing the span outright degrades the text the model has to reason
about, and a worse answer is the price paid twice.

**Known identity is removed first, patterns second.** We already hold the
candidate's name from local extraction. Matching it literally is exact where a
pattern is a guess, so it runs first and the guesses only mop up.

**A pattern that might be a skill is left alone.** "C#" contains a digit,
"OAuth 2.0" looks like a version, an employee number looks like a phone. The
rules here are deliberately narrow: over-redaction produces a CV that no longer
describes anyone's competence, which fails the purpose while looking safe.

Nothing here is a substitute for the scope check. This is the second lock, and
it exists because a boundary defended in one place is a boundary defended by
whoever remembers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["RedactionReport", "redact"]

#: Email. Deliberately strict — a loose one eats "SAP@2024" and version strings.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")

#: International and French phone shapes: +216 XX XXX XXX, 06 12 34 56 78,
#: (+33) 1 23 45 67 89.
#:
#: The separator class excludes newlines deliberately. With ``\s`` in it, a
#: phone at the end of one line swallowed the start of the next: "+216 55 123
#: 456" followed by "5-7 rue" came back as "[TEL] rue", eating a street number.
_PHONE = re.compile(r"(?<![\w.])(?:\+|\()?\d[\d ().-]{7,17}\d(?![\w.])")

#: A personal URL or handle. Company sites in a tender are public information,
#: so only the platforms that identify an individual are removed.
_HANDLE = re.compile(
    r"\b(?:https?://)?(?:www\.)?(?:linkedin\.com|github\.com|twitter\.com|x\.com|"
    r"facebook\.com|instagram\.com)/[\w./-]+",
    re.IGNORECASE,
)

#: French postal address line: 5 digits followed by a place name.
#:
#: The place name must be Capitalised-then-lowercase. Requiring only a capital
#: matched "45000 EUR" and redacted a contract value — five digits followed by
#: a currency code, a unit or an all-caps heading is not an address, and losing
#: a budget to an over-eager rule fails the purpose while looking safe.
_POSTAL = re.compile(
    r"\b\d{5}\s+[A-ZÀ-Þ][a-zà-þ][\w'\-]*(?:[\s-][A-ZÀ-Þ][a-zà-þ][\w'\-]*)*"
)

#: National identifiers. French INSEE (15 digits) and Tunisian CIN (8 digits
#: preceded by a label, because eight bare digits are far too common).
_NATIONAL_ID = re.compile(
    r"\b(?:CIN|C\.I\.N|NIR|INSEE|passeport|passport)\s*[:n°#]*\s*\d[\d ]{6,19}",
    re.IGNORECASE,
)

#: Date of birth, only when labelled. A bare date is a project milestone far
#: more often than a birthday.
#: The accent is optional: extraction strips accents on some PDFs, and
#: "Ne le 12/03/1998" must be caught as surely as "Né le".
_BIRTH = re.compile(
    r"\b(?:n[ée]e?\s+le|date\s+de\s+naissance|born|d\.?o\.?b\.?)\s*[:\s]*"
    r"\d{1,2}[/.\-\s]\d{1,2}[/.\-\s]\d{2,4}",
    re.IGNORECASE,
)


@dataclass(slots=True)
class RedactionReport:
    """The redacted text, and what was taken out of it.

    Counts rather than values: a log that records what was redacted has simply
    moved the personal data somewhere else.
    """

    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _apply(pattern: re.Pattern[str], token: str, text: str, counts: dict[str, int]) -> str:
    replaced, hits = pattern.subn(token, text)
    if hits:
        counts[token] = counts.get(token, 0) + hits
    return replaced


def redact(text: str, *, known_names: list[str] | None = None) -> RedactionReport:
    """Strip identifying spans, replacing each with a typed placeholder.

    ``known_names`` are identities already extracted locally — the candidate's
    name, chiefly. Matching them literally is exact, where every pattern below
    is an approximation, so they go first.
    """
    if not text:
        return RedactionReport(text="", counts={})

    counts: dict[str, int] = {}
    out = text

    # Structured identifiers first, names second. The reverse order looked
    # natural and leaked: replacing "RAMI OUALI" before the email rule ran
    # turned "rami.ouali@inetum.com" into "[NOM].[NOM]@inetum.com", which no
    # longer matches an email pattern — so the address was published with only
    # its local part masked.
    out = _apply(_EMAIL, "[EMAIL]", out, counts)
    out = _apply(_HANDLE, "[PROFIL]", out, counts)
    out = _apply(_NATIONAL_ID, "[IDENTIFIANT]", out, counts)
    out = _apply(_BIRTH, "[NAISSANCE]", out, counts)
    out = _apply(_POSTAL, "[ADRESSE]", out, counts)
    out = _apply(_PHONE, "[TEL]", out, counts)

    for name in known_names or []:
        cleaned = name.strip()
        if len(cleaned) < 2:
            continue
        # Word-boundary and case-insensitive: a CV writes "RAMI OUALI" in the
        # header and "R. Ouali" in a footer. Each part is removed separately so
        # the surname alone, appearing on its own, is caught too.
        for part in {cleaned, *cleaned.split()}:
            if len(part) < 3:
                continue
            out = _apply(
                re.compile(rf"\b{re.escape(part)}\b", re.IGNORECASE), "[NOM]", out, counts
            )

    return RedactionReport(text=out, counts=counts)
