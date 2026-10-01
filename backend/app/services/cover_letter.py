"""The covering letter — the first document the model writes rather than rewords.

Every other document in this module copies or reformulates something that
already exists. A letter has no source text, which removes the safety net the
CV adaptation relies on: there is nothing to fall back to, sentence by
sentence, when a passage goes wrong.

So the anchor moves. The letter's factual claims are drawn from the **compliance
matrix** — the technologies the retained team can actually evidence, computed
deterministically from a selection a human validated. The model is given that
list and told to write around it; anything it names that is not on the list is
a claim nobody can support, and the guard refuses the whole letter rather than
trying to mend a paragraph that has no correct version to return to.

Three consequences follow, and each is a decision rather than a detail.

**The gaps are never mentioned as strengths.** The matrix knows which
requirements are uncovered. They are deliberately *withheld* from the prompt
rather than listed as things to avoid: a model told "do not claim RGAA" writes
about RGAA. What it is not given, it does not reach for.

**Refusal is whole-letter.** For a CV, one bad paragraph reverts and the rest
stands. A letter is one argument; a sentence claiming a competence the firm
does not have poisons the document, and there is no honest fragment to keep.

**The fallback is a real letter.** No key, a timeout, a refused draft — the
platform assembles a plain, factual letter from the same brief. It reads like
a form, and a form that is true beats an eloquent paragraph nobody can stand
behind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["CoverLetter", "LetterBrief", "compose_letter"]

#: Paragraphs asked for. A covering letter that runs past three is read by
#: nobody and gives the model room to drift.
_PARAGRAPHS = 3
_MAX_PARAGRAPH_CHARS = 700

_SYSTEM = """Tu rédiges une lettre d'accompagnement pour une réponse à appel \
d'offres, au nom d'une société de services informatiques.

RÈGLES ABSOLUES :
1. Tu n'affirmes QUE ce que la note de cadrage fournit. Aucune compétence, \
technologie, certification, référence client, chiffre ou date qui n'y figure \
pas.
2. Tu n'inventes aucune réalisation passée. Si la note ne cite pas de \
référence, tu n'en évoques aucune.
3. Trois paragraphes courts : l'intérêt pour la mission, ce que l'équipe \
proposée apporte, la disponibilité et la suite.
4. Ton professionnel et sobre. Pas de superlatifs, pas de « leader », pas \
d'« excellence reconnue ».
5. Tu réponds UNIQUEMENT par un objet JSON, sans texte autour.

FORMAT DE RÉPONSE :
{"paragraphes": ["...", "...", "..."]}"""


@dataclass(slots=True)
class LetterBrief:
    """Everything the letter may assert, and nothing else."""

    buyer: str = ""
    reference: str = ""
    title: str = ""
    deadline: str = ""
    country: str = ""
    #: Names of the retained profiles, as the validator saw them.
    team: list[str] = field(default_factory=list)
    #: Technologies the team can evidence. The only competences the letter is
    #: allowed to name.
    covered: list[str] = field(default_factory=list)
    #: Counted, never listed. The model is not told what the gaps are, because
    #: a model told "do not mention RGAA" writes about RGAA.
    gaps: int = 0
    #: Style examples from past approved dossiers. Facts are never taken from
    #: them — they describe other people's work on another mission.
    examples: list[str] = field(default_factory=list)

    def as_prompt(self) -> str:
        lines = ["NOTE DE CADRAGE", ""]
        if self.title:
            lines.append(f"Mission : {self.title[:300]}")
        if self.buyer:
            lines.append(f"Acheteur : {self.buyer}")
        if self.reference:
            lines.append(f"Référence : {self.reference}")
        if self.deadline:
            lines.append(f"Date limite : {self.deadline}")
        if self.team:
            lines.append(f"Équipe proposée : {', '.join(self.team)}")
        if self.covered:
            lines.append(
                "Compétences attestées par l'équipe (les seules que tu peux citer) : "
                + ", ".join(self.covered)
            )
        if self.examples:
            lines += [
                "",
                "EXEMPLES DE STYLE (lettres validées — n'en reprends aucun fait) :",
            ]
            lines += [f"- {item[:300]}" for item in self.examples[:2]]
        return "\n".join(lines)


@dataclass(slots=True)
class CoverLetter:
    paragraphs: list[str]
    #: ``written`` · ``fallback`` · ``unavailable``
    status: str
    llm_used: bool = False
    #: Terms the model asserted that the brief did not support. Recorded for
    #: the audit trail of the rule firing, never for the document.
    refused_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "llm_used": self.llm_used,
            "refused_terms": self.refused_terms,
            "paragraphs": len(self.paragraphs),
        }


def compose_letter(brief: LetterBrief) -> CoverLetter:
    """Write the letter, or assemble a plain one that is certainly true."""
    from app.services.llm import get_llm

    result = get_llm().complete(
        system=_SYSTEM,
        user=brief.as_prompt(),
        # A letter carries the consultants' names and the client's. Declared
        # as `cv` so it answers to the sovereignty switch and the redaction
        # pass, exactly like the CV adaptation.
        kind="cv",
        max_tokens=900,
    )
    if not result.ok:
        logger.info("letter.unavailable", reason=result.reason)
        return CoverLetter(paragraphs=_fallback(brief), status="unavailable")

    payload = result.as_json()
    drafted = payload.get("paragraphes") if isinstance(payload, dict) else None
    paragraphs = [
        str(item).strip()[:_MAX_PARAGRAPH_CHARS]
        for item in (drafted or [])
        if str(item).strip()
    ][:_PARAGRAPHS]

    if not paragraphs:
        logger.info("letter.unusable_response")
        return CoverLetter(paragraphs=_fallback(brief), status="unavailable")

    refused = _unsupported_claims(" ".join(paragraphs), brief)
    if refused:
        # Whole-letter refusal. A letter is one argument; a sentence claiming
        # a competence the firm cannot evidence poisons the document, and
        # unlike a CV there is no honest fragment to keep.
        logger.warning("letter.refused", terms=refused)
        return CoverLetter(
            paragraphs=_fallback(brief), status="fallback", refused_terms=refused
        )

    logger.info("letter.written", paragraphs=len(paragraphs), redactions=result.redactions)
    return CoverLetter(paragraphs=paragraphs, status="written", llm_used=True)


# ---------------------------------------------------------------------------
def _unsupported_claims(text: str, brief: LetterBrief) -> list[str]:
    """Competences the letter asserts that the brief did not grant.

    Checked against the *covered* list rather than against a CV: the letter
    speaks for the team, so the question is not "did someone write this
    somewhere" but "can the proposed team evidence it".
    """
    from app.services.adaptation import _ACRONYM, _COMMON_ACRONYMS
    from app.services.document_qa import _PROMPT_LEAK
    from app.services.matching import required_technologies

    if _PROMPT_LEAK.search(text):
        return ["fragment_de_generation"]

    granted = {term.lower() for term in brief.covered}
    refused = [term for term in required_technologies(text) if term.lower() not in granted]

    granted_acronyms = {term.upper() for term in brief.covered}
    granted_acronyms |= {word.upper() for word in brief.buyer.split()}
    granted_acronyms |= {word.upper() for word in brief.reference.split()}
    granted_acronyms |= {word.upper() for word in brief.title.split()}
    refused += [
        token
        for token in _ACRONYM.findall(text)
        if token.upper() not in granted_acronyms and token.upper() not in _COMMON_ACRONYMS
    ]
    return sorted(set(refused))


def _fallback(brief: LetterBrief) -> list[str]:
    """A plain letter assembled from the brief.

    It reads like a form, and that is the point: every sentence is a
    restatement of a stored fact. A form that is true beats an eloquent
    paragraph nobody can stand behind.
    """
    designation = brief.title or "la consultation"
    first = (
        f"Nous avons pris connaissance de votre consultation « {designation[:200]} »"
        + (f" (référence {brief.reference})" if brief.reference else "")
        + " et souhaitons vous faire part de notre candidature."
    )

    if brief.team and brief.covered:
        second = (
            f"L'équipe que nous proposons réunit {len(brief.team)} consultant(s) — "
            f"{', '.join(brief.team)} — dont les compétences attestées couvrent "
            f"notamment : {', '.join(brief.covered[:10])}."
        )
    elif brief.team:
        second = (
            f"L'équipe que nous proposons réunit {len(brief.team)} consultant(s) : "
            f"{', '.join(brief.team)}. Le détail de leur parcours figure dans les "
            "curriculum vitæ joints."
        )
    else:
        second = (
            "Le détail des profils proposés figure dans les curriculum vitæ joints "
            "au présent dossier."
        )

    third = (
        "Les curriculum vitæ des intervenants et la matrice de conformité sont joints. "
        "Nous restons à votre disposition pour tout complément"
        + (f" avant le {brief.deadline}" if brief.deadline else "")
        + "."
    )
    return [first, second, third]
