"""The covering letter — the one document with no source text to fall back to.

Every other document copies or reformulates something that already exists. A
letter is written, which removes the net the CV adaptation relies on: there is
no correct earlier version of a sentence to restore.

So the tests are about the two things that replace it. The letter may only
assert what the compliance matrix granted, and when it asserts more the whole
draft is refused rather than mended — because a letter is one argument, and a
sentence claiming a competence the firm does not have poisons the rest.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.cover_letter import LetterBrief, compose_letter


def _brief(**overrides: Any) -> LetterBrief:
    base = {
        "buyer": "GIP OKANTIS",
        "reference": "AO 01/2026",
        "title": "Tierce maintenance applicative",
        "deadline": "31/03/2026",
        "team": ["Marie Dupont", "Ahmed Ben Ali"],
        "covered": ["Java", "Spring", "Docker"],
        "gaps": 4,
    }
    base.update(overrides)
    return LetterBrief(**base)


class _Reply:
    def __init__(self, payload, ok=True, reason=None):
        self.ok, self.reason, self._payload = ok, reason, payload
        self.redactions, self.content = {"NOM": 2}, "stub"

    def as_json(self):
        return self._payload


@pytest.fixture
def llm(monkeypatch):
    box: dict[str, Any] = {}

    def install(payload, ok=True, reason=None):
        class _Client:
            def complete(self, **kwargs):
                box["prompt"] = kwargs.get("user", "")
                box["kind"] = kwargs.get("kind")
                return _Reply(payload, ok=ok, reason=reason)

        monkeypatch.setattr("app.services.llm.get_llm", lambda: _Client())

    box["install"] = install
    return box


class TestTheLetterMayOnlyClaimWhatWasGranted:
    def test_an_unsupported_technology_refuses_the_whole_draft(self, llm):
        """No partial rescue. A letter is one argument; a claim the firm
        cannot evidence poisons the paragraphs around it."""
        llm["install"](
            {
                "paragraphes": [
                    "Nous avons pris connaissance de votre consultation.",
                    "Notre équipe maîtrise Java, Spring et Kubernetes.",
                    "Nous restons à votre disposition.",
                ]
            }
        )

        letter = compose_letter(_brief())

        assert letter.status == "fallback"
        assert letter.refused_terms == ["Kubernetes"]
        assert "Kubernetes" not in " ".join(letter.paragraphs)

    def test_an_unsupported_acronym_refuses_the_draft(self, llm):
        llm["install"](
            {
                "paragraphes": [
                    "Nous sommes certifiés RGAA et HDS pour cette mission.",
                ]
            }
        )

        letter = compose_letter(_brief())

        assert letter.status == "fallback"
        assert "RGAA" in letter.refused_terms

    def test_an_acronym_from_the_tender_itself_is_allowed(self, llm):
        """The buyer's own name and the reference are facts of the brief, not
        claims about the firm."""
        llm["install"](
            {
                "paragraphes": [
                    "Nous répondons à la consultation du GIP OKANTIS.",
                    "Notre équipe maîtrise Java et Docker.",
                ]
            }
        )

        letter = compose_letter(_brief())

        assert letter.status == "written"

    def test_a_draft_inside_the_brief_is_kept(self, llm):
        """The pass must be worth running: an honest letter has to survive."""
        llm["install"](
            {
                "paragraphes": [
                    "Nous avons pris connaissance de votre consultation.",
                    "L'équipe proposée réunit des compétences Java, Spring et Docker.",
                    "Nous restons à votre disposition.",
                ]
            }
        )

        letter = compose_letter(_brief())

        assert letter.status == "written"
        assert letter.llm_used is True
        assert len(letter.paragraphs) == 3

    def test_a_prompt_fragment_refuses_the_draft(self, llm):
        llm["install"]({"paragraphes": ["```json", "Notre équipe maîtrise Java."]})

        letter = compose_letter(_brief())

        assert letter.status == "fallback"


class TestTheGapsAreWithheldNotListed:
    def test_the_prompt_never_names_an_uncovered_requirement(self, llm):
        """A model told "do not claim RGAA" writes about RGAA. What it is not
        given, it does not reach for."""
        llm["install"]({"paragraphes": ["Nous maîtrisons Java."]})

        compose_letter(_brief(covered=["Java"], gaps=3))

        assert "RGAA" not in llm["prompt"]
        assert "non couverte" not in llm["prompt"].lower()

    def test_the_prompt_lists_only_what_may_be_claimed(self, llm):
        llm["install"]({"paragraphes": ["Nous maîtrisons Java."]})

        compose_letter(_brief())

        assert "Java, Spring, Docker" in llm["prompt"]


class TestTheFallbackIsARealLetter:
    """No key, a timeout, a refused draft — the platform still produces
    something a bid manager can send. It reads like a form, and a form that is
    true beats an eloquent paragraph nobody can stand behind."""

    @pytest.mark.parametrize(
        ("payload", "ok", "reason"),
        [
            (None, False, "disabled"),
            (None, False, "out_of_scope:cv"),
            (None, True, None),
            ({"paragraphes": []}, True, None),
        ],
    )
    def test_every_failure_still_yields_a_letter(self, llm, payload, ok, reason):
        llm["install"](payload, ok=ok, reason=reason)

        letter = compose_letter(_brief())

        assert letter.status == "unavailable"
        assert letter.llm_used is False
        assert len(letter.paragraphs) == 3

    def test_the_fallback_states_only_stored_facts(self, llm):
        llm["install"](None, ok=False, reason="disabled")

        body = " ".join(compose_letter(_brief()).paragraphs)

        assert "AO 01/2026" in body
        assert "Marie Dupont" in body
        assert "Java" in body

    def test_the_fallback_survives_an_empty_brief(self, llm):
        llm["install"](None, ok=False, reason="disabled")

        letter = compose_letter(LetterBrief())

        assert len(letter.paragraphs) == 3
        assert all(paragraph.strip() for paragraph in letter.paragraphs)


class TestTheSovereigntySwitchApplies:
    def test_the_letter_is_declared_as_cv_data(self, llm):
        """It carries the consultants' names, so it answers to the same scope
        switch and the same redaction pass as the CV adaptation."""
        llm["install"]({"paragraphes": ["Nous maîtrisons Java."]})

        compose_letter(_brief())

        assert llm["kind"] == "cv"

    def test_past_examples_are_offered_as_style_only(self, llm):
        llm["install"]({"paragraphes": ["Nous maîtrisons Java."]})

        compose_letter(_brief(examples=["Notre équipe a livré la refonte du portail."]))

        assert "n'en reprends aucun fait" in llm["prompt"]
