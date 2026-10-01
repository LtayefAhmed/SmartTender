"""Reading, checking and filling .docx templates.

A template is a Word document with named holes. ``docxtpl`` puts Jinja2 inside
the document body, so the file the Bureau d'Études uploads is not inert data —
it is a program. That single fact drives everything here.

**Every render runs in a sandbox.** A plain ``jinja2.Environment`` lets a
template reach ``{{ ''.__class__.__mro__ }}`` and from there to arbitrary code,
executed by the API process, on a file someone uploaded. ``SandboxedEnvironment``
closes that. It is not optional and it is not a precaution against a
hypothetical attacker: it is what makes "templates are data" a defensible claim
rather than a slogan.

**Variables are flat, and that is a design choice.** ``{{ nom }}`` rather than
``{{ identite.nom }}``. Jinja only reports top-level names, so a nested context
could be validated as far as ``identite`` and no further — every typo inside it
would survive upload and surface as a blank in a submitted document. Flat names
are checked exactly. They are also easier for whoever is editing the Word file,
who is not a developer.

The residual risk is worth stating plainly: a template can still loop for a very
long time, and nothing here interrupts it. Uploading a template is an
authenticated action by the organisation's own staff, and the sandbox stops it
becoming code execution — but a hostile template is a denial of service, not a
solved problem.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from app.core.enums import TemplateKind
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "VARIABLES",
    "TemplateError",
    "Variable",
    "check_variables",
    "discover_variables",
    "render_template",
    "variables_for",
]


class TemplateError(Exception):
    """A template that cannot be accepted or cannot be filled."""


@dataclass(frozen=True, slots=True)
class Variable:
    """One placeholder a template may use."""

    name: str
    #: ``text`` · ``list`` (of strings) · ``records`` (list of objects looped over)
    kind: str
    label: str
    example: str
    #: False when the platform declares the name but does not yet produce it.
    #: Such a variable is *accepted* in a template and renders empty, so the
    #: default CV template can be written once, in full, rather than rewritten
    #: when the structured career timeline lands.
    available: bool = True


#: What a CV template can ask for.
#:
#: `formations` and `experiences` are declared and not yet produced: reading a
#: dated career timeline out of a CV is its own piece of work, with its own
#: failure mode — a pattern that fires too readily invents a plausible wrong
#: date in a contractual document. Until it exists they render as empty loops.
_CV_VARIABLES: tuple[Variable, ...] = (
    Variable("nom", "text", "Nom du consultant", "Marie Dupont"),
    Variable("poste", "text", "Intitulé du poste", "Ingénieur études et développement"),
    Variable("fichier", "text", "Nom du CV source", "cv_dupont.pdf"),
    Variable("competences", "list", "Technologies attestées", "Java, Spring, Docker"),
    Variable("langues", "list", "Langues", "français, anglais"),
    Variable("certifications", "list", "Certifications", "AWS Certified, ITIL"),
    Variable("niveau_etudes", "text", "Niveau d'études", "Bac+5 (Master / Ingénieur)"),
    Variable(
        "formations",
        "records",
        "Formations datées — champs : annee, diplome, etablissement",
        "{% for f in formations %}{{ f.annee }} {{ f.diplome }}{% endfor %}",
        available=False,
    ),
    Variable(
        "experiences",
        "records",
        "Expériences datées — champs : debut, fin, poste, employeur, missions",
        "{% for e in experiences %}{{ e.poste }}{% endfor %}",
        available=False,
    ),
    # The mission the CV is being submitted for. A funder's CV form almost
    # always names it on the first line.
    Variable("mission_titre", "text", "Intitulé de la mission", "TMA du SI ARIA"),
    Variable("mission_acheteur", "text", "Acheteur", "Ministère des Technologies"),
    Variable("mission_reference", "text", "Référence de l'AO", "AO 01/2026"),
    Variable("mission_pays", "text", "Pays", "Tunisie"),
    Variable("mission_echeance", "text", "Date limite", "31/03/2026"),
    # Provenance of the selection this CV comes from.
    Variable("rang", "text", "Rang dans la sélection", "1"),
    Variable("score", "text", "Score de rapprochement", "0.81"),
    Variable("extraits", "list", "Extraits probants du CV", "Développement Java 17…"),
    Variable("genere_le", "text", "Date de génération", "27/08/2026"),
    #: Empty once the document is approved. A version awaiting validation must
    #: say so on its face — the parcours asks for the watermark explicitly, and
    #: a draft that looks final is how a draft gets sent.
    Variable("filigrane", "text", "Mention d'état", "GÉNÉRÉ — EN ATTENTE DE VALIDATION"),
)

#: What a compliance-matrix template can ask for.
#:
#: `lignes` is the table: one record per demand of the tender, each carrying
#: its status, the profiles that answer it and the evidence. The counters are
#: separate variables because the header states them before the table, and a
#: reader decides whether to read on from those three numbers.
#:
#: `a_traiter` is deliberately its own counter rather than folded into the
#: ratio. A commitment about method, price or presence on site cannot be
#: judged from a CV, and a denominator that swallowed those would turn an
#: unknown into a success on a contractual document.
_MATRIX_VARIABLES: tuple[Variable, ...] = (
    Variable(
        "lignes",
        "records",
        "Lignes de la matrice — champs : section, label, statut, profils, evidence, note",
        "{% for l in lignes %}{{ l.label }} — {{ l.statut }}{% endfor %}",
    ),
    Variable("couvertes", "text", "Exigences couvertes", "18"),
    Variable("non_couvertes", "text", "Exigences non couvertes", "6"),
    Variable("a_traiter", "text", "Exigences non évaluables depuis un CV", "13"),
    Variable("taux_couverture", "text", "Taux, sur les seules lignes évaluables", "75"),
    Variable("equipe", "list", "Profils retenus", "Marie Dupont, Ahmed Ben Ali"),
    Variable("mission_titre", "text", "Intitulé de la mission", "TMA du SI ARIA"),
    Variable("mission_acheteur", "text", "Acheteur", "Ministère des Technologies"),
    Variable("mission_reference", "text", "Référence de l'AO", "AO 01/2026"),
    Variable("mission_pays", "text", "Pays", "Tunisie"),
    Variable("mission_echeance", "text", "Date limite", "31/03/2026"),
    Variable("genere_le", "text", "Date de génération", "27/08/2026"),
    Variable("filigrane", "text", "Mention d'état", "GÉNÉRÉ — EN ATTENTE DE VALIDATION"),
)

#: What a covering-letter template can ask for.
#:
#: `paragraphes` is the body — written by the model from a brief, or assembled
#: deterministically when the model is unavailable or its draft was refused.
#: Everything else is a stored fact, so a template that only prints those
#: produces a letter that is plain and certainly true.
_LETTER_VARIABLES: tuple[Variable, ...] = (
    Variable(
        "paragraphes",
        "list",
        "Corps de la lettre, un paragraphe par entrée",
        "{% for p in paragraphes %}{{ p }}{% endfor %}",
    ),
    Variable("equipe", "list", "Profils retenus", "Marie Dupont, Ahmed Ben Ali"),
    Variable("competences", "list", "Compétences attestées par l'équipe", "Java · Docker"),
    Variable("mission_titre", "text", "Intitulé de la mission", "TMA du SI ARIA"),
    Variable("mission_acheteur", "text", "Acheteur", "Ministère des Technologies"),
    Variable("mission_reference", "text", "Référence de l'AO", "AO 01/2026"),
    Variable("mission_pays", "text", "Pays", "Tunisie"),
    Variable("mission_echeance", "text", "Date limite", "31/03/2026"),
    Variable("genere_le", "text", "Date de génération", "27/08/2026"),
    Variable("filigrane", "text", "Mention d'état", "GÉNÉRÉ — EN ATTENTE DE VALIDATION"),
)

#: Variables shared by every kind of document.
_COMMON: tuple[Variable, ...] = (
    Variable("mission_titre", "text", "Intitulé de la mission", "TMA du SI ARIA"),
    Variable("mission_acheteur", "text", "Acheteur", "Ministère des Technologies"),
    Variable("mission_reference", "text", "Référence de l'AO", "AO 01/2026"),
    Variable("mission_pays", "text", "Pays", "Tunisie"),
    Variable("mission_echeance", "text", "Date limite", "31/03/2026"),
    Variable("genere_le", "text", "Date de génération", "27/08/2026"),
    Variable("filigrane", "text", "Mention d'état", "GÉNÉRÉ — EN ATTENTE DE VALIDATION"),
)

VARIABLES: dict[str, tuple[Variable, ...]] = {
    TemplateKind.CV.value: _CV_VARIABLES,
    # The other kinds are declared with the common set only. Each will grow its
    # own catalogue when its data source exists — which for the financial form
    # means a pricing domain the platform does not have.
    TemplateKind.EXPERT_SHEET.value: _CV_VARIABLES,
    TemplateKind.COVER_LETTER.value: _LETTER_VARIABLES,
    TemplateKind.COMPLIANCE_MATRIX.value: _MATRIX_VARIABLES,
    TemplateKind.TECHNICAL_FORM.value: _COMMON,
    TemplateKind.FINANCIAL_FORM.value: _COMMON,
}


def variables_for(kind: str) -> tuple[Variable, ...]:
    """The catalogue for one kind of document, or the common set."""
    return VARIABLES.get(kind, _COMMON)


# ---------------------------------------------------------------------------
def _environment() -> Any:
    """A Jinja environment a template cannot escape from.

    ``SandboxedEnvironment`` blocks attribute access that reaches the object
    graph — ``__class__``, ``__globals__``, ``__subclasses__`` — which is the
    path from "someone uploaded a Word file" to "someone ran code on the API".
    """
    from jinja2.sandbox import SandboxedEnvironment

    # Jinja's default `Undefined` is deliberately kept, and the choice was
    # measured rather than assumed. It already renders a missing name, a
    # missing record field and a loop over a missing list as blanks — the
    # leniency a funder's form needs — while a sandbox violation still raises
    # loudly, because the blocked attribute is returned as an undefined whose
    # *chained* access fails with `SecurityError`.
    #
    # `ChainableUndefined` was tried and rejected: it swallows that chain too,
    # so an attempted escape renders as an empty string and leaves no trace.
    # A blocked attack that says nothing is a blocked attack nobody learns from.
    return SandboxedEnvironment(autoescape=False)


def discover_variables(content: bytes) -> set[str]:
    """The placeholders a .docx asks for.

    Only top-level names, because that is all Jinja reports. Inside a loop over
    ``experiences``, the fields of each record are attribute access and are
    invisible here — which is why the record-shaped variables document their
    fields in their label.
    """
    from docxtpl import DocxTemplate

    try:
        document = DocxTemplate(io.BytesIO(content))
        found = document.get_undeclared_template_variables(_environment())
    except Exception as exc:
        # A corrupt archive, a file that is not a .docx at all, or Jinja
        # refusing to parse a malformed tag. All of them mean the same thing to
        # the person uploading: this file cannot be used.
        raise TemplateError(f"Ce fichier n'est pas un gabarit exploitable : {exc}") from exc

    return {str(name) for name in found}


def check_variables(found: set[str], *, kind: str) -> tuple[list[str], list[str]]:
    """Split what a template asks for into unknown and not-yet-produced.

    Returns ``(unknown, unavailable)``. Unknown is a rejection: the platform has
    no way to fill it and never will without a code change, so accepting the
    template would only defer the failure to generation time. Unavailable is a
    warning: the name is real, the data is not there yet, and the section will
    render empty until it is.
    """
    catalogue = {variable.name: variable for variable in variables_for(kind)}
    unknown = sorted(name for name in found if name not in catalogue)
    unavailable = sorted(
        name for name in found if name in catalogue and not catalogue[name].available
    )
    return unknown, unavailable


def render_template(content: bytes, context: dict[str, Any]) -> bytes:
    """Fill a template and return the .docx bytes.

    The context is passed as given. Callers build it from stored data only —
    never from a prompt, never from anything a scraped document said — so that
    what lands in a submitted file traces back to a row someone can point at.
    """
    from docxtpl import DocxTemplate

    try:
        document = DocxTemplate(io.BytesIO(content))
        document.render(context, _environment())
        buffer = io.BytesIO()
        document.save(buffer)
    except Exception as exc:
        raise TemplateError(f"Le gabarit n'a pas pu être rempli : {exc}") from exc

    rendered = buffer.getvalue()
    logger.info(
        "template.rendered",
        bytes_in=len(content),
        bytes_out=len(rendered),
        variables=len(context),
    )
    return rendered
