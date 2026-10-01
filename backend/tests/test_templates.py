"""Templates as data — reading them, refusing them, and filling them.

A template is a Word file someone uploads that contains executable Jinja. That
makes this module the one place in the platform where untrusted input is, by
design, a program. The tests that matter are therefore not about rendering
nicely; they are about the two ways this feature can betray the guarantee the
parcours makes — *scraped or uploaded content is data, never instructions*:

* a template must not be able to reach the Python object graph;
* a template must not be able to ask for something the platform cannot fill and
  have that discovered only when a document is due.

The rest — versions, empty loops, unavailable variables — is behaviour that is
easy to break silently while everything still looks like it works.
"""

from __future__ import annotations

import io

import pytest

from app.services.templates import (
    TemplateError,
    check_variables,
    discover_variables,
    render_template,
    variables_for,
)


def _docx(paragraphs: list[str]) -> bytes:
    """A minimal real .docx carrying the given paragraphs."""
    from docx import Document

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class TestATemplateCannotEscapeIntoPython:
    """`docxtpl` runs Jinja over a file a user uploaded. Without a sandbox that
    is arbitrary code execution in the API process, triggered by a Word
    document — which is exactly the attack the platform's guarantee rules out.
    """

    def test_reaching_the_class_hierarchy_is_refused(self):
        from jinja2.exceptions import SecurityError

        template = _docx(["{{ ''.__class__.__mro__[1].__subclasses__() }}"])

        with pytest.raises(TemplateError) as caught:
            render_template(template, {})

        # The sandbox is what refused it, not a coincidence of a missing name.
        assert isinstance(caught.value.__cause__, SecurityError)

    def test_reaching_a_function_globals_is_refused(self):
        from jinja2.exceptions import SecurityError

        template = _docx(["{{ nom.__init__.__globals__ }}"])

        with pytest.raises(TemplateError) as caught:
            render_template(template, {"nom": "Marie"})

        assert isinstance(caught.value.__cause__, SecurityError)

    def test_an_ordinary_template_still_renders(self):
        """The sandbox must not be so tight that legitimate templates break."""
        rendered = render_template(
            _docx(["Bonjour {{ nom }}", "{{ competences | join(', ') }}"]),
            {"nom": "Marie Dupont", "competences": ["Java", "Spring"]},
        )

        assert "Marie Dupont" in _read(rendered)
        assert "Java, Spring" in _read(rendered)


class TestAMissingValueIsABlankNotAFailure:
    """A funder's form has fields we cannot always fill. A raised exception
    five minutes before a deadline is a document nobody gets; a blank is a hole
    the QA pass can name."""

    def test_an_absent_variable_renders_empty(self):
        rendered = render_template(_docx(["[{{ nom }}]"]), {})

        assert "[]" in _read(rendered)

    def test_a_loop_over_an_absent_list_renders_nothing(self):
        """`formations` and `experiences` are declared and not yet produced.
        The default template loops over both today."""
        rendered = render_template(
            _docx(["[{% for f in formations %}{{ f.diplome }}{% endfor %}]"]), {}
        )

        assert "[]" in _read(rendered)

    def test_a_missing_field_inside_a_record_renders_empty(self):
        rendered = render_template(
            _docx(["[{% for e in experiences %}{{ e.poste }}/{{ e.absent }}{% endfor %}]"]),
            {"experiences": [{"poste": "Dev"}]},
        )

        assert "[Dev/]" in _read(rendered)


class TestVariablesAreCheckedAtTheDoor:
    def test_the_placeholders_are_read_out_of_the_file(self):
        found = discover_variables(_docx(["{{ nom }} — {{ poste }}"]))

        assert found == {"nom", "poste"}

    def test_an_unknown_name_is_reported(self):
        """Refused at upload, while the author is still holding the file.
        Telling them at generation time, three weeks later, is how a tool gets
        abandoned."""
        unknown, _ = check_variables({"nom", "salaire_souhaite"}, kind="cv")

        assert unknown == ["salaire_souhaite"]

    def test_a_declared_but_unproduced_name_is_a_warning_not_a_refusal(self):
        """`formations` is real, and empty until the structured timeline lands.
        Rejecting it would force the default template to be rewritten later
        instead of simply filling itself."""
        unknown, unavailable = check_variables({"nom", "formations"}, kind="cv")

        assert unknown == []
        assert unavailable == ["formations"]

    def test_a_file_that_is_not_a_docx_is_refused_clearly(self):
        with pytest.raises(TemplateError):
            discover_variables(b"%PDF-1.4 this is not a word document")

    def test_a_malformed_jinja_tag_is_refused(self):
        with pytest.raises(TemplateError):
            discover_variables(_docx(["{% for x in %}"]))

    def test_every_catalogued_variable_has_a_label_and_an_example(self):
        """The catalogue is served to whoever writes the Word file. A name with
        no explanation is a name they will guess wrong."""
        for variable in variables_for("cv"):
            assert variable.label
            assert variable.example
            assert variable.kind in {"text", "list", "records"}


class TestTheDefaultTemplate:
    """Built by a script rather than committed as a binary, so a reviewer can
    see what changed. These tests are what stop the script drifting from the
    catalogue it must satisfy."""

    @pytest.fixture
    def built(self, tmp_path):
        from scripts.build_default_template import build

        return build(tmp_path / "cv.docx").read_bytes()

    def test_it_only_asks_for_variables_the_platform_declares(self, built):
        unknown, _ = check_variables(discover_variables(built), kind="cv")

        assert unknown == []

    def test_it_renders_a_complete_cv(self, built):
        text = _read(
            render_template(
                built,
                {
                    "filigrane": "GÉNÉRÉ — EN ATTENTE DE VALIDATION",
                    "nom": "Marie Dupont",
                    "poste": "Ingénieure études et développement",
                    "niveau_etudes": "Bac+5",
                    "mission_titre": "TMA du SI ARIA",
                    "mission_acheteur": "Ministère des Technologies",
                    "mission_reference": "AO 01/2026",
                    "mission_pays": "Tunisie",
                    "mission_echeance": "31/03/2026",
                    "competences": ["Java", "Spring"],
                    "langues": ["français", "anglais"],
                    "certifications": ["AWS Certified"],
                    "formations": [],
                    "experiences": [],
                    "genere_le": "27/08/2026",
                    "rang": 1,
                    "score": 0.81,
                },
            )
        )

        assert "Marie Dupont" in text
        assert "TMA du SI ARIA" in text
        assert "Java · Spring" in text
        # The watermark is on the face of the document, not in its metadata.
        assert "EN ATTENTE DE VALIDATION" in text

    def test_the_career_sections_are_empty_until_the_timeline_exists(self, built):
        """Not a defect: the headings stand, the content arrives with the
        structured extraction. Asserted so that the day it lands, this test
        fails and someone updates it deliberately."""
        found = discover_variables(built)

        assert {"formations", "experiences"} <= found


def _read(content: bytes) -> str:
    from docx import Document

    document = Document(io.BytesIO(content))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        parts.extend(cell.text for row in table.rows for cell in row.cells)
    return "\n".join(parts)
