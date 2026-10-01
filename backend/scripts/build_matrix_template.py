"""Build the default compliance-matrix template.

Generated from this file rather than committed as a ``.docx``, for the same
reason as the CV template: a Word document in git is an opaque binary, and
nobody reviewing a pull request can tell whether a column was renamed or a
section dropped. Here the layout is the diff.

The one interesting mechanic is the table. ``docxtpl`` repeats a table row when
the row's first cell opens a loop with ``{%tr for ... %}`` — the tag's own row
disappears and the body repeats once per record. Written as an ordinary
``{% for %}`` the whole table would render into a single cell.

Run from ``backend/``::

    python scripts/build_matrix_template.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_OUT = Path("config/templates/matrice_conformite.docx")

#: Printed above the table. The three counters are separated on purpose: a
#: reader has to see how much of the dossier the platform could not judge
#: before they read a coverage percentage, or the percentage misleads them.
_HEADER = (
    ("Intitulé", "{{ mission_titre }}"),
    ("Acheteur", "{{ mission_acheteur }}"),
    ("Référence", "{{ mission_reference }}"),
    ("Date limite", "{{ mission_echeance }}"),
    ("Équipe retenue", "{{ equipe | join(', ') }}"),
)


def build(destination: Path) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    document = Document()

    # Landscape A4: the table has six columns and one of them holds quoted
    # evidence. Portrait would wrap every cell to three lines.
    for section in document.sections:
        section.page_width = Cm(29.7)
        section.page_height = Cm(21.0)
        for margin in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
            setattr(section, margin, Cm(1.6))

    document.styles["Normal"].font.name = "Calibri"
    document.styles["Normal"].font.size = Pt(9)

    banner = document.add_paragraph()
    banner.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = banner.add_run("{{ filigrane }}")
    run.bold = True
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0xB0, 0x30, 0x30)

    title = document.add_paragraph()
    run = title.add_run("MATRICE DE CONFORMITÉ")
    run.bold = True
    run.font.size = Pt(16)

    header = document.add_table(rows=0, cols=2)
    header.style = "Table Grid"
    for label, placeholder in _HEADER:
        cells = header.add_row().cells
        cells[0].paragraphs[0].add_run(label).bold = True
        cells[1].text = placeholder

    document.add_paragraph()
    summary = document.add_paragraph()
    summary.add_run(
        "{{ couvertes }} exigence(s) couverte(s) · "
        "{{ non_couvertes }} non couverte(s) · "
        "{{ taux_couverture }} % de couverture"
    ).bold = True
    caveat = document.add_paragraph()
    run = caveat.add_run(
        "{{ a_traiter }} exigence(s) ne sont pas évaluables à partir des CV "
        "(méthode, prix, engagements) et ne sont pas comptées dans le taux. "
        "Elles restent à rédiger."
    )
    run.italic = True
    run.font.size = Pt(8.5)
    run.font.color.rgb = RGBColor(0x70, 0x70, 0x70)

    document.add_paragraph()

    table = document.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for index, label in enumerate(
        ("Section", "Exigence", "Statut", "Profils", "Éléments probants", "Commentaire")
    ):
        cell = table.rows[0].cells[index]
        cell.paragraphs[0].add_run(label).bold = True

    # Three rows, not one. `{%tr %}` deletes the row that carries it, so
    # opening and closing the loop in the same row deletes the body with them —
    # which is exactly what the first attempt did, and docxtpl reported it only
    # as "Encountered unknown tag 'endfor'".
    #
    # An ordinary `{% for %}` is also wrong here: it would render the whole
    # table into a single cell.
    table.add_row().cells[0].text = "{%tr for l in lignes %}"

    body = table.add_row().cells
    body[0].text = "{{ l.section }}"
    body[1].text = "{{ l.label }}"
    body[2].text = "{{ l.statut }}"
    body[3].text = "{{ l.profils }}"
    body[4].text = "{{ l.evidence }}"
    body[5].text = "{{ l.note }}"

    table.add_row().cells[0].text = "{%tr endfor %}"

    document.add_paragraph()
    footer = document.add_paragraph()
    run = footer.add_run(
        "Généré le {{ genere_le }} · établi à partir de la sélection validée · "
        "aucune ligne n'est déduite : une exigence est dite couverte uniquement "
        "lorsqu'un profil retenu l'atteste dans son CV."
    )
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(".docx.part")
    document.save(staging)
    staging.replace(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    path = build(args.out)

    from app.services.templates import check_variables, discover_variables

    found = discover_variables(path.read_bytes())
    unknown, unavailable = check_variables(found, kind="matrice_conformite")

    print(f"écrit      {path} ({path.stat().st_size:,} octets)")
    print(f"variables  {', '.join(sorted(found))}")
    if unknown:
        print(f"INCONNUES  {', '.join(unknown)}")
        return 1
    if unavailable:
        print(f"en attente {', '.join(unavailable)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
