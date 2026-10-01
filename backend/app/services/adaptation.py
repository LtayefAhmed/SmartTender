"""Adapting a CV to a tender — reformulating and selecting, never inventing.

Inetum's solution slide promises "enrichissement automatique". The parcours
guarantees that "le LLM n'invente jamais une compétence". Taken literally those
two sentences contradict each other, and the contradiction is resolved by a
decision, not by wording:

    REFORMULATE  "dev Java/Spring" → "conception d'applications
                 transactionnelles J2EE"          the skill exists, the
                                                  vocabulary moves        yes
    SELECT       surface the 4 relevant roles out of 15, drop the rest
                                                  nothing added, the
                                                  order changes           yes
    COMPLETE     "did Spring Boot" → "masters microservices"
                                                  inference               never

A bailleur CV is a contractual document; the expert can be interviewed on it. A
skill inferred and then collapsing in an interview costs far more than the time
it saved.

**The rule is enforced mechanically, not by prompting.** Asking a model nicely
not to invent is a hope. Every reformulated passage is checked back against its
source: any technology named in the output that is absent from the input causes
that passage to be discarded and the original kept. Same for years — a model
that writes "depuis 2015" over a role that started in 2018 has fabricated a
date in a document someone signs.

Three nets, and each claims only what it was measured to hold.

**Technologies and acronyms** absent from the source are a hard rejection.
Deterministic, language-invariant, and they close the shape of invention that
matters most: a model importing a requirement — "RGAA" — into a CV to make it
look like a match. Observed on the very first real run.

**Years** absent from the source are a hard rejection. Dates on a funder's form
are checked.

**Semantic support** claims far less than I first built it to. A fidelity gate
at 0.65 rejected eight good passages out of eight, because comparing a long
source to a short reformulation measures compression, and dropping detail is
allowed. Per-sentence support measured better but still does not separate
cleanly — 0.064 between an invented claim and a faithful compression is a coin
toss, not a threshold. So it is split: an absurdity floor that rejects a
sentence supported by nothing at all, and a flag that names thin sentences for
the reviewer without touching them.

The honest limit, stated rather than glossed: this pass can still soften a fact
or write a vague sentence nobody can disprove. The toggle is off by default,
every document is a watermarked draft, thin sentences are named, and a human
signs. That is the net.

Failure is always silent and always safe. No key, a timeout, malformed JSON, a
rejected passage — the original text is kept and the document is produced. The
pass is an improvement, never a dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["Adaptation", "adapt_experiences"]

#: Roles submitted to the model. More than this and the prompt grows past what
#: the reformulation is worth: the model reads a career, not an archive.
_MAX_SUBMITTED = 10

#: A reformulation that triples the source is not a reformulation. Measured
#: against the 600-character mission box the extractor already enforces.
_MAX_GROWTH = 1.6

#: Non-capturing on purpose. With a capturing group, `findall` returns the
#: group — "19", "20" — rather than the year, and a set built from it would
#: contain no year at all while looking like it did.
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")

#: An all-caps token: RGAA, TMA, SEO, KPI, ERP. Language-invariant, which is
#: what makes it usable here — the corpus is English and the output French, so
#: every ordinary-prose comparison is defeated by translation.
_ACRONYM = re.compile(r"\b[A-Z]{2,6}\b")

#: Per-sentence support, measured against the best-matching sentence of the
#: source. Two thresholds, because one measurement cannot carry both jobs.
#:
#: Measured on real pairs from the first run:
#:     invented "e-commerce"   0.176
#:     unrelated (accounting)  0.193
#:     faithful compression    0.257
#:     hollowed out            0.342
#:     faithful (SaaS)         0.418
#:
#: 0.064 between an invention and a faithful compression is not a threshold. So
#: `_UNSUPPORTED` is an absurdity floor — a sentence backed by nothing anywhere
#: in the source — and `_THIN` only marks a passage for the reviewer without
#: touching it. Fitting a stricter gate to five points would look rigorous and
#: would reject good work.
_UNSUPPORTED = 0.22
_THIN = 0.45

#: A sentence shorter than this carries too little signal for the comparison to
#: mean anything.
_MIN_SENTENCE = 25

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")

#: Acronyms a reformulation may introduce without claiming anything: they name
#: the document, not a competence. Kept deliberately short — every addition is
#: a hole in the guard, so a term earns its place here only by being unable to
#: read as a skill.
_COMMON_ACRONYMS = {"CV", "AO", "SI", "R&D"}

_SYSTEM = """Tu es un assistant qui adapte des expériences professionnelles \
au vocabulaire d'un appel d'offres.

RÈGLES ABSOLUES :
1. Tu ne peux QUE reformuler et sélectionner. Tu n'ajoutes JAMAIS une \
compétence, une technologie, une certification, une date ou un chiffre qui \
n'est pas déjà écrit dans le texte source.
2. Si une exigence de l'appel d'offres n'apparaît pas dans une expérience, tu \
ne l'y écris pas. Tu baisses simplement sa pertinence.
3. Tu écris en français, au style d'un CV professionnel : phrases courtes, \
verbes d'action, pas de superlatifs.
4. Tu réponds UNIQUEMENT par un objet JSON, sans texte autour.

FORMAT DE RÉPONSE :
{"experiences": [{"index": 0, "pertinence": 85, "missions": "..."}]}

`index` reprend le numéro fourni. `pertinence` est un entier de 0 à 100 \
mesurant l'adéquation avec l'appel d'offres. `missions` est la reformulation, \
plus courte ou de longueur comparable à la source."""


@dataclass(slots=True)
class Adaptation:
    """The result, and enough about it to explain the document later."""

    experiences: list[dict[str, Any]]
    #: ``adapted`` · ``unavailable`` · ``unusable`` · ``no_input``
    status: str
    llm_used: bool = False
    reformulated: int = 0
    #: Passages the guard sent back, with the original kept.
    rejected: int = 0
    #: What the guard caught, for the audit trail. Terms, never whole passages.
    invented_terms: list[str] = field(default_factory=list)
    #: Sentences the model wrote that rest on thin support in the source. Kept
    #: in the document — they are not provably wrong — and named here so a
    #: reviewer reads them first.
    flagged: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "llm_used": self.llm_used,
            "reformulated": self.reformulated,
            "rejected": self.rejected,
            "invented_terms": self.invented_terms,
            "flagged": self.flagged,
        }


def adapt_experiences(
    experiences: list[dict[str, Any]],
    *,
    requirements: list[str] | None = None,
    limit: int = 8,
    past_responses: list[str] | None = None,
    similarity: Any = None,
) -> Adaptation:
    """Reformulate and reorder a career against one tender's requirements.

    ``past_responses`` is the seam for the validated-response referential: once
    that collection has content, retrieved passages join the prompt as *style*
    examples. It is accepted and unused today rather than added later, so the
    call sites do not change when the collection fills.

    ``similarity(a, b) -> float`` is injected rather than imported. It needs the
    multilingual encoder, the encoder is 470 MB resident, and loading it in the
    API process is the mistake that took the host down once already. Passed in,
    the caller decides where the model lives — and the tests can exercise the
    rule without one. Omitted, the semantic check is skipped and only the
    lexical guards apply; the report says so.
    """
    if not experiences:
        return Adaptation(experiences=[], status="no_input")

    from app.services.llm import get_llm

    client = get_llm()
    submitted = experiences[:_MAX_SUBMITTED]

    result = client.complete(
        system=_SYSTEM,
        user=_prompt(submitted, requirements or [], past_responses or []),
        kind="cv",
        max_tokens=2_000,
    )
    if not result.ok:
        # Disabled, out of scope, timed out, refused. All the same to the
        # caller: keep what we had and produce the document.
        logger.info("adaptation.skipped", reason=result.reason)
        return Adaptation(experiences=experiences[:limit], status="unavailable")

    payload = result.as_json()
    entries = payload.get("experiences") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        logger.info("adaptation.unusable_response")
        return Adaptation(experiences=experiences[:limit], status="unusable")

    adapted, reformulated, rejected, invented, flagged = _verify(
        submitted, entries, similarity
    )

    # Selection: relevance decides the order, and the original order breaks
    # ties so a model returning flat scores does not shuffle a career at random.
    adapted.sort(key=lambda item: (-item["_pertinence"], item["_index"]))
    kept = [{k: v for k, v in item.items() if not k.startswith("_")} for item in adapted]
    # Roles the model did not return keep their place at the end rather than
    # vanishing: silence from a model is not a decision to drop someone's job.
    returned = {item["_index"] for item in adapted}
    kept += [exp for position, exp in enumerate(submitted) if position not in returned]

    logger.info(
        "adaptation.completed",
        submitted=len(submitted),
        reformulated=reformulated,
        rejected=rejected,
        invented=len(invented),
        flagged=len(flagged),
        redactions=result.redactions,
    )
    return Adaptation(
        experiences=kept[:limit],
        status="adapted",
        llm_used=True,
        reformulated=reformulated,
        rejected=rejected,
        invented_terms=invented,
        flagged=flagged,
    )


# ---------------------------------------------------------------------------
def _prompt(
    experiences: list[dict[str, Any]],
    requirements: list[str],
    past_responses: list[str],
) -> str:
    parts: list[str] = []

    if requirements:
        parts.append("EXIGENCES DE L'APPEL D'OFFRES :")
        parts += [f"- {item}" for item in requirements[:12]]
        parts.append("")

    if past_responses:
        # Style only. Never facts: a past response describes other people's
        # work, and borrowing a fact from it would be inventing one here.
        parts.append("EXEMPLES DE STYLE (réponses validées, ne pas copier les faits) :")
        parts += [f"- {item[:300]}" for item in past_responses[:3]]
        parts.append("")

    parts.append("EXPÉRIENCES À ADAPTER :")
    for index, item in enumerate(experiences):
        header = " · ".join(
            part
            for part in (
                f"{item.get('debut', '')} – {item.get('fin', '')}".strip(" –"),
                str(item.get("poste") or ""),
                str(item.get("employeur") or ""),
            )
            if part
        )
        parts.append(f"[{index}] {header}")
        parts.append(str(item.get("missions") or "")[:800])
        parts.append("")

    return "\n".join(parts)


def _verify(
    submitted: list[dict[str, Any]], entries: list[Any], similarity: Any = None
) -> tuple[list[dict[str, Any]], int, int, list[str], list[str]]:
    """Keep a reformulation only when it says nothing the source did not."""
    from app.services.matching import required_technologies

    adapted: list[dict[str, Any]] = []
    reformulated = rejected = 0
    invented: list[str] = []
    flagged: list[str] = []
    seen: set[int] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if not isinstance(index, int) or not 0 <= index < len(submitted):
            continue
        if index in seen:
            # A model returning the same role twice would duplicate a job in
            # the document. First answer wins.
            continue
        seen.add(index)

        original = dict(submitted[index])
        proposal = str(entry.get("missions") or "").strip()
        pertinence = entry.get("pertinence")
        pertinence = int(pertinence) if isinstance(pertinence, (int, float)) else 0

        source = " ".join(
            str(original.get(field) or "")
            for field in ("poste", "employeur", "missions", "debut", "fin")
        )
        problem, thin = _rejection_reason(
            proposal, source, required_technologies, similarity=similarity
        )

        if problem is None:
            original["missions"] = proposal
            reformulated += 1
            flagged.extend(thin)
        else:
            rejected += 1
            if isinstance(problem, list):
                invented.extend(problem)
            logger.info("adaptation.passage_rejected", index=index, reason=str(problem)[:80])

        original["_index"] = index
        original["_pertinence"] = pertinence
        adapted.append(original)

    return adapted, reformulated, rejected, sorted(set(invented)), flagged[:6]


def _rejection_reason(
    proposal: str, source: str, technologies: Any, *, similarity: Any = None
) -> tuple[Any, list[str]]:
    """Returns ``(problem, thin_sentences)``.

    ``problem`` is ``None`` to accept, otherwise a reason — or the list of
    invented terms — to refuse. ``thin_sentences`` are kept but named for the
    reviewer.

    The lexical nets are deterministic and language-invariant, which matters
    because the corpus is English and the output French: no word-overlap rule
    on ordinary prose survives translation. The semantic net only claims an
    absurdity floor, for the reason set out at `_UNSUPPORTED`.
    """
    if not proposal:
        return "empty", []

    # A reformulation that triples its source has stopped reformulating.
    if len(proposal) > max(200, len(source) * _MAX_GROWTH):
        return "too_long", []

    # The guard that matters. A technology named in the output and absent from
    # the input is a competence the model added.
    held = {term.lower() for term in technologies(source)}
    added = [term for term in technologies(proposal) if term.lower() not in held]

    # Acronyms, for everything the curated lexicon does not carry. The observed
    # failure: a model pulled "RGAA" out of the tender's requirements and wrote
    # it into a role that never mentioned it — the most dangerous shape of
    # invention, because it is precisely what makes the CV look like a match.
    source_acronyms = set(_ACRONYM.findall(source.upper()))
    added += [
        token
        for token in _ACRONYM.findall(proposal)
        if token.upper() not in source_acronyms and token.upper() not in _COMMON_ACRONYMS
    ]
    if added:
        return sorted(set(added)), []

    # A year that is not in the source is a fabricated date, and dates in a
    # funder's CV form are checked.
    source_years = set(_YEAR.findall(source))
    for year in _YEAR.findall(proposal):
        if year not in source_years:
            return "invented_year", []

    if similarity is None:
        return None, []

    # Sentence by sentence, against the best match anywhere in the source.
    # Comparing whole texts measured compression rather than drift: a faithful
    # summary of a long paragraph scored as low as an invented one, and the
    # first version of this rule rejected eight good passages out of eight.
    try:
        supports = [
            (sentence, max(float(similarity(candidate, sentence)) for candidate in sources))
            for sentence in _sentences(proposal)
            for sources in [_sentences(source) or [source]]
        ]
    except Exception:
        # A model that fails to load must not block a document. The lexical
        # guards already ran; this one abstains.
        return None, []

    thin: list[str] = []
    for sentence, support in supports:
        if support < _UNSUPPORTED:
            return f"unsupported:{support:.2f}", []
        if support < _THIN:
            thin.append(sentence[:180])
    return None, thin


def _sentences(text: str) -> list[str]:
    parts = (part.strip() for part in _SENTENCE.split(text or ""))
    return [part for part in parts if len(part) >= _MIN_SENTENCE]
