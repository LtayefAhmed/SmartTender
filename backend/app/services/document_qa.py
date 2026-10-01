"""Checking a produced document before anyone reads it.

The parcours asks for four controls — completeness, conformity to the funder's
format, coherence of the data, absence of prompt fragments — and for a verdict
*per section*, so that a failure costs one paragraph rather than a whole
regeneration.

Everything here inspects the **rendered document**, not the intention behind
it. A context that looked right and a template that silently dropped half of it
produce a correct-looking report and a broken file; reading the artefact is the
only check that cannot be fooled that way.

Two decisions shape the module.

**Severity is binary and means something.** ``bloquant`` is "this must not
leave the platform" — an unrendered placeholder, a model's own words in the
prose, an employment date in the future. ``avertissement`` is "a human should
look" — a missing section, a suspiciously old year. Inflating warnings into
blockers would make the gate theatre; people route around a gate that cries
wolf.

**The repair is a fallback, not a retry.** The parcours calls for "régénération
ciblée". Asking the model again for a passage it just got wrong may get it
wrong differently, and the loop has no guaranteed end. Reverting that one
passage to the text the CV actually contains is targeted, cheap, and
terminates — the deterministic version passes these checks by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["Finding", "QaReport", "repair_context", "review"]

BLOCKING = "bloquant"
WARNING = "avertissement"

#: A Jinja marker that survived rendering means the template did not run — the
#: reader would receive `{{ nom }}` where a name belongs.
_UNRENDERED = re.compile(r"\{\{|\}\}|\{%")

#: Things a language model says that a CV never does. Each one was chosen
#: because it survives translation and casing, and because no legitimate
#: professional prose contains it.
#:
#: The word boundary is applied per alternative, not to the group. Written as
#: ``\b(?:…|```|…)`` it silently matched nothing that starts with punctuation:
#: a backtick is not a word character, so ``\b`` demands a transition that a
#: fenced code block at the start of a passage cannot provide. The pattern
#: compiled, the tests read convincingly, and a JSON fence sailed through.
_PROMPT_LEAK = re.compile(
    r"\ben tant qu'(?:assistant|ia|intelligence)"
    r"|\bas an ai\b"
    r"|\bje ne peux pas\b"
    r"|\bi cannot\b"
    r"|\bdésolé[, ]"
    r"|\bi'm sorry\b"
    r"|\bvoici (?:le|la|les|une|un) (?:reformulation|réponse|json)"
    r"|```"
    r"|\"pertinence\""
    r"|\"missions\"\s*:"
    r"|\"index\"\s*:",
    re.IGNORECASE,
)

#: A year before this is a transcription error far more often than a career.
_IMPLAUSIBLE_BEFORE = 1950

#: Fields without which the document is not the document. Deliberately short:
#: every addition turns a human judgement into a machine refusal.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "cv": ("nom", "experiences"),
    "fiche_expert": ("nom",),
    "matrice_conformite": ("lignes",),
}

_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


@dataclass(slots=True)
class Finding:
    """One thing wrong with the document, and where."""

    check: str
    severity: str
    #: ``experiences[2]``, ``formations``, ``document``. The regeneration loop
    #: reads this to know what to put back, so it names a place and not a
    #: feeling.
    section: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "section": self.section,
            "message": self.message,
        }


@dataclass(slots=True)
class QaReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == BLOCKING]

    @property
    def passed(self) -> bool:
        return not self.blocking

    @property
    def failed_sections(self) -> list[str]:
        """Sections to put back, in the order the loop should treat them."""
        seen: list[str] = []
        for finding in self.blocking:
            if finding.section not in seen:
                seen.append(finding.section)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocking": len(self.blocking),
            "warnings": len(self.findings) - len(self.blocking),
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
def review(text: str, *, context: dict[str, Any], kind: str = "cv") -> QaReport:
    """Inspect a rendered document against the data it was built from.

    ``text`` is the document's own words, extracted from the .docx — not the
    context. A template that dropped a section renders a file the context
    cannot describe, and that is exactly the failure worth catching.
    """
    findings: list[Finding] = []
    findings += _check_rendering(text)
    findings += _check_prompt_leak(text, context)
    findings += _check_completeness(text, context, kind)
    findings += _check_dates(context)

    report = QaReport(findings=findings)
    logger.info(
        "qa.reviewed",
        kind=kind,
        passed=report.passed,
        blocking=len(report.blocking),
        warnings=len(findings) - len(report.blocking),
    )
    return report


def repair_context(
    context: dict[str, Any], report: QaReport, source_experiences: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Put the failing passages back to what the CV actually says.

    Returns the corrected context and the sections that were reverted.

    This is the "régénération ciblée" of the parcours, done by falling back
    rather than re-asking. A model that produced a prompt fragment once may
    produce a different one on retry, and the loop would have no guaranteed
    end; the source text passes these checks by construction, so one pass is
    always enough.
    """
    repaired = dict(context)
    reverted: list[str] = []

    experiences = list(repaired.get("experiences") or [])
    by_position = {f"experiences[{index}]": index for index in range(len(experiences))}

    for section in report.failed_sections:
        index = by_position.get(section)
        if index is None:
            continue
        original = _match_source(experiences[index], source_experiences)
        if original is None:
            continue
        entry = dict(experiences[index])
        entry["missions"] = str(original.get("missions") or "")
        experiences[index] = entry
        reverted.append(section)

    if reverted:
        repaired["experiences"] = experiences
        logger.info("qa.repaired", sections=reverted)
    return repaired, reverted


# ---------------------------------------------------------------------------
def _check_rendering(text: str) -> list[Finding]:
    if not _UNRENDERED.search(text):
        return []
    return [
        Finding(
            check="format",
            severity=BLOCKING,
            section="document",
            message=(
                "Le document contient des marqueurs de gabarit non remplacés "
                "({{ … }}). Le gabarit n'a pas été rendu."
            ),
        )
    ]


def _check_prompt_leak(text: str, context: dict[str, Any]) -> list[Finding]:
    """Model wording that reached the page.

    Located per experience when possible, so the repair knows which paragraph
    to put back instead of discarding the whole adaptation.
    """
    findings: list[Finding] = []
    for index, entry in enumerate(context.get("experiences") or []):
        match = _PROMPT_LEAK.search(str(entry.get("missions") or ""))
        if match:
            findings.append(
                Finding(
                    check="prompt",
                    severity=BLOCKING,
                    section=f"experiences[{index}]",
                    message=f"Fragment de génération détecté : « {match.group(0)[:40]} ».",
                )
            )

    # Anywhere else in the document, the section cannot be narrowed.
    located = {f.section for f in findings}
    match = _PROMPT_LEAK.search(text)
    if match and not located:
        findings.append(
            Finding(
                check="prompt",
                severity=BLOCKING,
                section="document",
                message=f"Fragment de génération détecté : « {match.group(0)[:40]} ».",
            )
        )
    return findings


def _check_completeness(text: str, context: dict[str, Any], kind: str) -> list[Finding]:
    findings: list[Finding] = []

    for name in _REQUIRED.get(kind, ()):
        if not context.get(name):
            findings.append(
                Finding(
                    check="completude",
                    severity=BLOCKING,
                    section=name,
                    message=f"« {name} » est vide — le document ne peut pas être envoyé ainsi.",
                )
            )

    # The name has to be on the page, not merely in the context: a template
    # that forgot to print it produces a CV about nobody.
    name = str(context.get("nom") or "").strip()
    if name and name not in text:
        findings.append(
            Finding(
                check="completude",
                severity=BLOCKING,
                section="nom",
                message="Le nom du profil n'apparaît pas dans le document produit.",
            )
        )

    for name in ("langues", "certifications", "formations", "niveau_etudes"):
        if name in context and not context.get(name):
            findings.append(
                Finding(
                    check="completude",
                    severity=WARNING,
                    section=name,
                    message=f"Rubrique « {name} » vide : à compléter avant envoi.",
                )
            )
    return findings


def _check_dates(context: dict[str, Any]) -> list[Finding]:
    """Dates that cannot be true.

    A funder checks these against the expert's record, so an end date before
    its start, or a year in the future, is not a cosmetic defect.
    """
    findings: list[Finding] = []
    this_year = datetime.now(timezone.utc).year

    for index, entry in enumerate(context.get("experiences") or []):
        section = f"experiences[{index}]"
        start = _year(str(entry.get("debut") or ""))
        end = _year(str(entry.get("fin") or ""))

        if start and end and end < start:
            findings.append(
                Finding(
                    check="coherence",
                    severity=BLOCKING,
                    section=section,
                    message=f"Période incohérente : {start} – {end}.",
                )
            )
        for year in (start, end):
            if year and year > this_year + 1:
                findings.append(
                    Finding(
                        check="coherence",
                        severity=BLOCKING,
                        section=section,
                        message=f"Date dans le futur : {year}.",
                    )
                )
            elif year and year < _IMPLAUSIBLE_BEFORE:
                findings.append(
                    Finding(
                        check="coherence",
                        severity=WARNING,
                        section=section,
                        message=f"Date improbable : {year}.",
                    )
                )

    for index, entry in enumerate(context.get("formations") or []):
        year = _year(str(entry.get("annee") or ""))
        if year and year > this_year + 1:
            findings.append(
                Finding(
                    check="coherence",
                    severity=BLOCKING,
                    section=f"formations[{index}]",
                    message=f"Diplôme daté dans le futur : {year}.",
                )
            )
    return findings


def _year(value: str) -> int | None:
    match = _YEAR.search(value)
    return int(match.group(0)) if match else None


def _match_source(
    entry: dict[str, Any], source: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the untouched version of one role.

    Matched on dates and title rather than position: the adaptation reorders
    by relevance, so index 2 of the output is rarely index 2 of the input.
    """
    for candidate in source:
        if candidate.get("debut") == entry.get("debut") and candidate.get(
            "poste"
        ) == entry.get("poste"):
            return candidate
    return None
