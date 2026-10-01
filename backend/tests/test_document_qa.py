"""The automatic review, and the correction loop that must terminate.

A gate is only worth having if it refuses things. These tests are about the
four failures that must stop a document — an unrendered template, a model's
own words in the prose, a missing name, an impossible date — and about the one
property that keeps the loop safe: falling back to the CV's own text always
passes, so the second pass is always the last.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.document_qa import WARNING, repair_context, review

_THIS_YEAR = datetime.now(timezone.utc).year


def _context(**overrides):
    base = {
        "nom": "Marie Dupont",
        "experiences": [
            {
                "debut": "2019",
                "fin": "2022",
                "poste": "Développeuse Java",
                "employeur": "",
                "missions": "Migration du socle applicatif.",
            }
        ],
        "formations": [{"annee": "2018", "diplome": "Diplôme d'ingénieur"}],
        "langues": ["français"],
        "certifications": ["AWS Certified"],
        "niveau_etudes": "Bac+5",
    }
    base.update(overrides)
    return base


def _text(context) -> str:
    """A rendered document that matches the context."""
    parts = [context.get("nom", "")]
    for entry in context.get("experiences") or []:
        parts.append(f"{entry['debut']} – {entry['fin']} · {entry.get('poste', '')}")
        parts.append(str(entry.get("missions") or ""))
    return "\n".join(parts)


class TestWhatMustNeverLeaveThePlatform:
    def test_an_unrendered_template_blocks(self):
        """`{{ nom }}` on the page means the template did not run, and the
        reader receives a placeholder where a name belongs."""
        context = _context()
        report = review("{{ nom }}\nCURRICULUM VITAE", context=context)

        assert not report.passed
        assert any(f.check == "format" for f in report.blocking)

    def test_a_prompt_fragment_blocks_and_names_its_paragraph(self):
        """The section matters as much as the verdict: the repair needs to
        know which paragraph to put back."""
        context = _context(
            experiences=[
                {
                    "debut": "2019",
                    "fin": "2022",
                    "poste": "Dev",
                    "missions": "Voici la reformulation demandée : migration du socle.",
                }
            ]
        )
        report = review(_text(context), context=context)

        assert not report.passed
        assert report.failed_sections == ["experiences[0]"]

    def test_a_missing_name_blocks(self):
        report = review("CURRICULUM VITAE\nMISSION", context=_context())

        assert not report.passed
        assert any(f.section == "nom" for f in report.blocking)

    def test_a_date_in_the_future_blocks(self):
        """A funder checks these against the expert's record."""
        context = _context(
            experiences=[
                {"debut": "2019", "fin": str(_THIS_YEAR + 5), "poste": "Dev", "missions": "x"}
            ]
        )
        report = review(_text(context), context=context)

        assert not report.passed
        assert any("futur" in f.message for f in report.blocking)

    def test_an_end_before_its_start_blocks(self):
        context = _context(
            experiences=[{"debut": "2022", "fin": "2019", "poste": "Dev", "missions": "x"}]
        )
        report = review(_text(context), context=context)

        assert any(f.check == "coherence" for f in report.blocking)

    def test_a_diploma_dated_in_the_future_blocks(self):
        context = _context(formations=[{"annee": str(_THIS_YEAR + 3), "diplome": "Master"}])
        report = review(_text(context), context=context)

        assert not report.passed


class TestWhatOnlyWarns:
    """Inflating a warning into a blocker makes the gate theatre, and people
    route around a gate that cries wolf."""

    def test_an_empty_optional_section_warns(self):
        context = _context(certifications=[], langues=[])
        report = review(_text(context), context=context)

        assert report.passed
        assert all(f.severity == WARNING for f in report.findings)

    def test_an_implausibly_old_year_warns(self):
        context = _context(
            experiences=[{"debut": "1912", "fin": "1915", "poste": "Dev", "missions": "x"}]
        )
        report = review(_text(context), context=context)

        assert report.passed
        assert any("improbable" in f.message for f in report.findings)

    def test_a_clean_document_passes_silently(self):
        context = _context()
        report = review(_text(context), context=context)

        assert report.passed
        assert report.to_dict()["blocking"] == 0


class TestTheCorrectionLoopTerminates:
    """The parcours asks for targeted regeneration. Re-asking the model may
    get it wrong differently and the loop would have no guaranteed end;
    putting the CV's own text back passes by construction, so one pass is
    always enough."""

    def test_a_failing_paragraph_reverts_to_the_source(self):
        source = [
            {
                "debut": "2019",
                "fin": "2022",
                "poste": "Développeuse Java",
                "missions": "Migration du socle applicatif Java.",
            }
        ]
        context = _context(
            experiences=[
                {
                    "debut": "2019",
                    "fin": "2022",
                    "poste": "Développeuse Java",
                    "missions": "```json {\"pertinence\": 90}```",
                }
            ]
        )
        report = review(_text(context), context=context)
        repaired, reverted = repair_context(context, report, source)

        assert reverted == ["experiences[0]"]
        assert repaired["experiences"][0]["missions"] == "Migration du socle applicatif Java."

    def test_the_repaired_document_passes(self):
        """The property that bounds the loop at two passes."""
        source = [
            {"debut": "2019", "fin": "2022", "poste": "Dev", "missions": "Migration du socle."}
        ]
        context = _context(
            experiences=[
                {"debut": "2019", "fin": "2022", "poste": "Dev", "missions": "Désolé, je"}
            ]
        )
        repaired, _ = repair_context(context, review(_text(context), context=context), source)

        assert review(_text(repaired), context=repaired).passed

    def test_the_source_is_matched_on_its_dates_not_its_position(self):
        """The adaptation reorders by relevance, so index 2 of the output is
        rarely index 2 of the input. Matching by position would restore the
        wrong job's text into the wrong job."""
        source = [
            {"debut": "2015", "fin": "2018", "poste": "Junior", "missions": "Maintenance."},
            {"debut": "2019", "fin": "2022", "poste": "Senior", "missions": "Migration."},
        ]
        context = _context(
            experiences=[
                {"debut": "2019", "fin": "2022", "poste": "Senior", "missions": "```"},
                {"debut": "2015", "fin": "2018", "poste": "Junior", "missions": "Maintenance."},
            ]
        )
        repaired, _ = repair_context(context, review(_text(context), context=context), source)

        assert repaired["experiences"][0]["missions"] == "Migration."

    def test_an_unmatched_paragraph_is_left_alone_rather_than_guessed(self):
        context = _context(
            experiences=[{"debut": "2019", "fin": "2022", "poste": "Dev", "missions": "```"}]
        )
        repaired, reverted = repair_context(context, review(_text(context), context=context), [])

        assert reverted == []
        assert repaired["experiences"][0]["missions"] == "```"

    @pytest.mark.parametrize("kind", ["cv", "fiche_expert", "matrice_conformite"])
    def test_every_kind_has_a_required_set(self, kind):
        report = review("", context={}, kind=kind)

        assert not report.passed
