"""The compliance matrix: what the tender demands, against what we can evidence.

The most useful document of the whole module, and the only one that needs no
model at all. Everything it says is already frozen in the shortlist — the
requirement passages read from the dossier, the technologies demanded, and the
evidence each retained profile was ranked on. Producing it is arithmetic over
rows somebody already approved.

**Its value is the empty cells.** A matrix that shows green everywhere tells a
bid manager nothing they did not already assume. The column that earns the
document is "not covered": the technology nobody on the team evidences, found
before the bid is written rather than during the defence.

Which forces the rule this module is built on. Measured on a real tender, the
dossier's obligations read:

    "Expertise RGAA"
    "Décrire la méthode envisagée pour réaliser la prestation"
    "Estimer le coût global nécessaire pour la réalisation"
    "Être capable d'intervenir sur site au siège de Limoges"

Only the first is about people at all. The others are about a method, a price
and a logistics commitment — nothing in a CV can answer them. A matrix that
quietly skipped those and reported "92% de couverture" would be worse than no
matrix: it would be a false assurance on a contractual document.

So the rows are split by **what the platform is able to judge**, the coverage
ratio counts only the assessable ones, and the rest are listed as work a human
still owes. Saying "I cannot assess this" is the feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["ComplianceMatrix", "MatrixRow", "build_matrix"]

#: How many evidence passages travel with a row. A matrix is read across, not
#: down: two quotations prove the point, ten make the page unreadable.
_MAX_EVIDENCE = 2
_EVIDENCE_CHARS = 220


@dataclass(slots=True)
class MatrixRow:
    """One demand of the tender, and what answers it."""

    #: ``technologie`` — mechanically assessable from the frozen evidence.
    #: ``exigence`` — read from the dossier, not answerable from a CV alone.
    section: str
    label: str
    #: ``couverte`` · ``non_couverte`` · ``a_traiter``
    #: ``a_traiter`` is never dressed up as either of the other two.
    statut: str
    #: Retained profiles that evidence it, as printed on the document.
    profils: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    #: For a row the platform cannot judge, what a human still has to do.
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "label": self.label,
            "statut": self.statut,
            "profils": ", ".join(self.profils),
            "evidence": " / ".join(self.evidence),
            "note": self.note,
        }


@dataclass(slots=True)
class ComplianceMatrix:
    rows: list[MatrixRow]
    #: Counted over assessable rows only. A denominator that silently included
    #: what cannot be judged would turn an unknown into a success.
    couvertes: int
    non_couvertes: int
    #: Stated beside the ratio, never folded into it.
    a_traiter: int
    profils: list[str]

    @property
    def taux(self) -> int:
        total = self.couvertes + self.non_couvertes
        return round(self.couvertes * 100 / total) if total else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "lignes": [row.to_dict() for row in self.rows],
            "couvertes": self.couvertes,
            "non_couvertes": self.non_couvertes,
            "a_traiter": self.a_traiter,
            "taux_couverture": self.taux,
            "profils": self.profils,
        }


def build_matrix(shortlist: Any, entries: list[Any]) -> ComplianceMatrix:
    """Cross the tender's demands with the retained team's evidence.

    ``entries`` are the *retained* shortlist entries. Nothing is recomputed:
    the technologies, the passages and the scores all come from the capture a
    human validated, so the matrix describes the decision that was taken rather
    than a fresh one that might differ.
    """
    profils = [str(getattr(entry, "label", "") or "") for entry in entries]
    rows: list[MatrixRow] = []

    rows += _technology_rows(shortlist, entries)
    rows += _obligation_rows(shortlist, entries)

    couvertes = sum(1 for row in rows if row.statut == "couverte")
    non_couvertes = sum(1 for row in rows if row.statut == "non_couverte")
    a_traiter = sum(1 for row in rows if row.statut == "a_traiter")

    matrix = ComplianceMatrix(
        rows=rows,
        couvertes=couvertes,
        non_couvertes=non_couvertes,
        a_traiter=a_traiter,
        profils=profils,
    )
    logger.info(
        "compliance.matrix_built",
        rows=len(rows),
        couvertes=couvertes,
        non_couvertes=non_couvertes,
        a_traiter=a_traiter,
        taux=matrix.taux,
    )
    return matrix


# ---------------------------------------------------------------------------
def _technology_rows(shortlist: Any, entries: list[Any]) -> list[MatrixRow]:
    """One row per technology the tender names.

    Mechanically assessable, and the only part of the matrix that is: a
    technology either appears in a profile's evidenced list or it does not.
    """
    demanded = [str(item) for item in (getattr(shortlist, "required_technologies", None) or [])]
    rows: list[MatrixRow] = []
    for technology in demanded:
        holders = [
            entry
            for entry in entries
            if technology.lower()
            in {str(t).lower() for t in (getattr(entry, "matched_technologies", None) or [])}
        ]
        quotes = _passages(holders, technology)

        if not holders:
            note = "Aucun profil retenu ne l'atteste."
        elif not quotes:
            # Covered but unquoted: the technology is in the profile's
            # evidenced list, and none of the passages the ranking kept
            # happens to name it. Saying so beats an empty cell a reader
            # reads as a defect — and beats filling it with an unrelated
            # quotation, which is what the first version did.
            note = "Attesté dans le CV, hors des passages retenus par le classement."
        else:
            note = ""

        rows.append(
            MatrixRow(
                section="Technologies exigées",
                label=technology,
                statut="couverte" if holders else "non_couverte",
                profils=[str(getattr(e, "label", "")) for e in holders],
                evidence=quotes,
                note=note,
            )
        )
    return rows


def _obligation_rows(shortlist: Any, entries: list[Any]) -> list[MatrixRow]:
    """One row per obligation read out of the dossier.

    None of these is assessable from a CV. "Décrire la méthode envisagée" and
    "Estimer le coût global" are answered by a proposal, not by a person. They
    are listed as work owed rather than scored, and where an obligation happens
    to name a technology the team evidences, that is offered as a starting
    point — a pointer, never a claim of compliance.
    """
    structured = getattr(shortlist, "structured_requirements", None) or {}
    obligations = [str(item) for item in (structured.get("exigences") or []) if str(item).strip()]
    if not obligations:
        return []

    from app.services.matching import required_technologies

    held: dict[str, list[str]] = {}
    for entry in entries:
        for technology in getattr(entry, "matched_technologies", None) or []:
            held.setdefault(str(technology).lower(), []).append(
                str(getattr(entry, "label", ""))
            )

    rows: list[MatrixRow] = []
    for obligation in obligations:
        touched = [
            (term, held[term.lower()])
            for term in required_technologies(obligation)
            if term.lower() in held
        ]
        note = "À rédiger : aucune compétence attestée ne répond directement."
        profils: list[str] = []
        if touched:
            terms = ", ".join(term for term, _ in touched)
            profils = sorted({name for _, names in touched for name in names})
            note = f"Piste : {terms} attesté(e)(s) par l'équipe — à confirmer par un humain."

        rows.append(
            MatrixRow(
                section="Exigences du dossier",
                label=obligation[:400],
                # Never "couverte". A CV cannot answer a commitment about
                # method, price or presence on site, and pretending otherwise
                # is the failure this document exists to prevent.
                statut="a_traiter",
                profils=profils,
                note=note,
            )
        )
    return rows


def _passages(entries: list[Any], technology: str) -> list[str]:
    """Quotations that actually mention the technology, or nothing.

    The first version returned each profile's highest-scoring passage whatever
    the row was about, so the same quotation appeared beside TypeScript,
    Docker, Kubernetes and GitLab — a column headed "éléments probants"
    showing text that proved none of them.

    A cell left empty says "the technology is evidenced in the CV, but not in
    the passages this ranking kept". That is true and checkable. A quotation
    that does not contain the word is neither.
    """
    needle = technology.lower()
    scored: list[tuple[float, str]] = []
    for entry in entries:
        label = str(getattr(entry, "label", ""))
        for item in getattr(entry, "evidence", None) or []:
            if not isinstance(item, dict):
                continue
            passage = str(item.get("passage") or "").strip(" :;-–—\n\t")
            if not passage or needle not in passage.lower():
                continue
            quote = f"{label} — {passage[:_EVIDENCE_CHARS]}"
            scored.append((float(item.get("score") or 0.0), quote))

    scored.sort(key=lambda pair: -pair[0])
    return [text for _, text in scored[:_MAX_EVIDENCE]]
