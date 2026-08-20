"""Repairing what OCR produced, and reading requirements out of prose.

Two jobs a language model does well and a rule cannot.

**Cleaning a scan.** Tesseract on a stamped, multi-script Tunisian notice
returns readable French interleaved with fragments like
``haloll ‏التجارة و تنمية‎ aylig`` — Arabic glyphs half-transliterated into
Latin. No regular expression tells that apart from a genuine reference number.
A model reading the surrounding sentence does.

**Structuring requirements.** A CCTP states in prose what a candidate must
have. Turning that into fields is what the technology lock and the eventual
CV-to-requirement comparison consume.

Both are refinements. The deterministic result is what is stored; the model's
answer replaces it only when it is well-formed and plausible, and the checks
below are what "plausible" means. A model that returns something shorter than
what it was given has summarised rather than cleaned, and a summary silently
loses the requirement that mattered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["RefinedText", "refine_ocr_text", "structure_requirements"]

_CLEAN_SYSTEM = (
    "Tu répares du texte issu d'un OCR de document administratif. "
    "Corrige les caractères mal reconnus, recolle les mots coupés, supprime "
    "les fragments illisibles et les en-têtes répétés. "
    "RÈGLES ABSOLUES : ne résume pas, ne reformule pas, n'ajoute rien, "
    "ne traduis pas. Conserve chaque phrase, chaque montant, chaque date, "
    "chaque référence et chaque nom de technologie à l'identique. "
    "Conserve les marqueurs entre crochets comme [NOM] ou [EMAIL] tels quels. "
    "Réponds uniquement avec le texte corrigé."
)

_REQUIREMENTS_SYSTEM = (
    "Tu lis un extrait de cahier des charges public. Extrais uniquement ce "
    "qu'il EXIGE d'un candidat ou d'un intervenant. "
    "Réponds en JSON strict, sans texte autour, avec ce schéma :\n"
    '{"technologies": [], "certifications": [], "experience_min_annees": null, '
    '"langues": [], "profils": [], "exigences": []}\n'
    "Chaque tableau ne contient que des CHAÎNES DE CARACTÈRES, jamais d'objets. "
    "technologies : noms de produits ou langages cités comme requis. "
    "profils : intitulés de poste demandés, un intitulé court par entrée. "
    "exigences : phrases courtes reprenant une obligation, en français. "
    "N'invente rien : un champ sans information reste vide ou null."
)

#: A cleaned document that lost more than this share of its length was
#: summarised, not repaired. Measured against real OCR: genuine cleanup removes
#: page furniture and duplicated headers, which is a few percent, never a third.
_MIN_LENGTH_RATIO = 0.75


@dataclass(slots=True)
class RefinedText:
    text: str
    changed: bool
    reason: str | None = None


def refine_ocr_text(
    text: str, *, kind: str = "tender", known_names: list[str] | None = None
) -> RefinedText:
    """Repair OCR output. Returns the original unchanged on any doubt.

    The length check is the whole safety of this function. Asked to clean, a
    model will sometimes summarise instead — the result reads beautifully and
    has quietly dropped the clause that named the required technology. Refusing
    anything materially shorter costs a few genuine repairs and prevents a
    class of loss nobody would notice.
    """
    from app.services.llm import get_llm

    if not text or len(text.strip()) < 200:
        return RefinedText(text=text, changed=False, reason="too_short")

    result = get_llm().complete(
        system=_CLEAN_SYSTEM,
        user=text,
        kind=kind,
        known_names=known_names,
        max_tokens=4000,
    )
    if not result.ok:
        return RefinedText(text=text, changed=False, reason=result.reason)

    cleaned = result.content.strip()
    if not cleaned:
        return RefinedText(text=text, changed=False, reason="empty_response")

    ratio = len(cleaned) / max(len(text), 1)
    if ratio < _MIN_LENGTH_RATIO:
        logger.warning("refinement.rejected_as_summary", ratio=round(ratio, 2))
        return RefinedText(text=text, changed=False, reason="looks_summarised")

    return RefinedText(text=cleaned, changed=True)


def structure_requirements(
    text: str, *, kind: str = "tender", known_names: list[str] | None = None
) -> dict[str, Any] | None:
    """Read a passage's requirements into fields. ``None`` when unavailable.

    Returning ``None`` rather than an empty structure keeps "the model was off"
    distinguishable from "this passage requires nothing" — two facts a caller
    must not conflate, since one is worth retrying and the other is not.
    """
    from app.services.llm import get_llm

    if not text or len(text.strip()) < 80:
        return None

    result = get_llm().complete(
        system=_REQUIREMENTS_SYSTEM,
        user=text,
        kind=kind,
        known_names=known_names,
        max_tokens=900,
    )
    parsed = result.as_json()
    if not isinstance(parsed, dict):
        return None

    # Normalised so a caller never has to guard every field. A model that
    # returns a string where a list belongs is common and not worth a retry.
    shape: dict[str, Any] = {
        "technologies": [],
        "certifications": [],
        "experience_min_annees": None,
        "langues": [],
        "profils": [],
        "exigences": [],
    }
    for key, default in shape.items():
        value = parsed.get(key, default)
        if isinstance(default, list):
            shape[key] = _as_strings(value)
        else:
            shape[key] = value if isinstance(value, int) else None
    return shape


#: Keys a model reaches for when it answers with an object where a string was
#: asked for, most helpful first.
_LABEL_KEYS = ("intitule", "intitulé", "nom", "name", "label", "titre", "profil")


def _as_strings(value: Any) -> list[str]:
    """Flatten a model's answer into a list of short labels.

    Asked for ``["Expert RGAA"]`` a model will sometimes return
    ``[{"intitule": "Expert RGAA", "niveau_experience": "avéré"}]`` — richer
    than requested and useless to a caller expecting text. Stringifying the
    dict whole put ``{'intitule': 'Expert RGAA', ...}`` on screen. Reading the
    label out of it recovers the answer instead of discarding it, because the
    model was right about the content and only wrong about the shape.
    """
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            label = next(
                (str(item[k]) for k in _LABEL_KEYS if item.get(k)),
                "",
            )
        else:
            label = str(item)
        label = label.strip()
        # A label longer than a line is a sentence that belongs in `exigences`.
        if label and len(label) <= 120 and label not in out:
            out.append(label)
    return out
