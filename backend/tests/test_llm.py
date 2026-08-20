"""The three locks between a document and a hosted model.

Scope decides whether a *kind* of document may be sent. Redaction decides what
of it may be sent. Bounds decide how much. Each catches what the others do not,
and these tests exercise them from the outside — by asking the client to do
something it must refuse, and checking what actually reached the wire.

Nothing here touches the network. A test that calls a paid API is a test that
gets disabled the first time the quota runs out.
"""

from __future__ import annotations

import pytest

from app.core.config import reset_settings_cache
from app.services.llm import LlmResult, MistralClient
from app.services.refinement import refine_ocr_text, structure_requirements


@pytest.fixture()
def configured(monkeypatch):
    """A client with a key, scoped to tenders only, and no network."""

    def _make(scope: str = "tenders", key: str = "test-key"):
        monkeypatch.setenv("SMARTTENDER_LLM__MISTRAL_API_KEY", key)
        monkeypatch.setenv("SMARTTENDER_LLM__SCOPE", scope)
        reset_settings_cache()
        return MistralClient()

    yield _make
    reset_settings_cache()


def _capture(client, monkeypatch, reply: str = "réponse"):
    """Record what the client would have sent, and answer without a network."""
    sent: dict[str, str] = {}

    def _post(system, user, max_tokens):
        sent["system"] = system
        sent["user"] = user
        return reply

    monkeypatch.setattr(client, "_post", _post)
    return sent


class TestScopeIsCheckedBeforeAnythingIsPrepared:
    def test_a_cv_is_refused_when_the_scope_is_tenders_only(self, configured, monkeypatch):
        client = configured(scope="tenders")
        sent = _capture(client, monkeypatch)

        result = client.complete(system="s", user="Jean Dupont", kind="cv")

        assert result.ok is False
        assert result.reason == "out_of_scope:cv"
        # Nothing was prepared, let alone sent.
        assert sent == {}

    def test_a_tender_is_allowed_under_the_same_scope(self, configured, monkeypatch):
        client = configured(scope="tenders")
        _capture(client, monkeypatch)

        assert client.complete(system="s", user="Avis d'appel", kind="tender").ok is True

    def test_both_are_allowed_once_the_scope_says_so(self, configured, monkeypatch):
        client = configured(scope="tenders_and_cvs")
        _capture(client, monkeypatch)

        assert client.complete(system="s", user="x" * 100, kind="cv").ok is True

    def test_an_unknown_kind_fails_closed(self, configured, monkeypatch):
        """A future third document type must be refused until someone decides,
        not admitted because the check did not think of it."""
        client = configured(scope="tenders_and_cvs")
        _capture(client, monkeypatch)

        assert client.complete(system="s", user="x", kind="photo").ok is False

    def test_no_key_disables_everything(self, configured, monkeypatch):
        client = configured(key="")
        sent = _capture(client, monkeypatch)

        result = client.complete(system="s", user="x", kind="tender")

        assert result.ok is False
        assert result.reason == "disabled"
        assert sent == {}


class TestNothingIdentifyingReachesTheWire:
    def test_the_payload_is_redacted_before_sending(self, configured, monkeypatch):
        """The lock that matters. Scope decides *whether*; this decides *what*."""
        client = configured(scope="tenders_and_cvs")
        sent = _capture(client, monkeypatch)

        client.complete(
            system="s",
            user="RAMI OUALI — rami.ouali@inetum.com — +216 55 123 456 — Docker",
            kind="cv",
            known_names=["RAMI OUALI"],
        )

        assert "OUALI" not in sent["user"].upper()
        assert "@" not in sent["user"]
        assert "55 123 456" not in sent["user"]
        # And the competence survives, or the call was pointless.
        assert "Docker" in sent["user"]

    def test_a_tender_is_redacted_too(self, configured, monkeypatch):
        """A notice names its buyer's contact — a person, with an email, who
        consented to nothing either."""
        client = configured(scope="tenders")
        sent = _capture(client, monkeypatch)

        client.complete(system="s", user="Contact : achats@ville.fr", kind="tender")

        assert "achats@ville.fr" not in sent["user"]

    def test_the_report_counts_without_recording_values(self, configured, monkeypatch):
        client = configured(scope="tenders_and_cvs")
        _capture(client, monkeypatch)

        result = client.complete(
            system="s", user="a@b.fr et Docker", kind="cv"
        )

        assert result.redactions == {"[EMAIL]": 1}


class TestBoundsHold:
    def test_the_payload_is_truncated_to_the_ceiling(self, configured, monkeypatch):
        monkeypatch.setenv("SMARTTENDER_LLM__MAX_INPUT_CHARS", "500")
        client = configured(scope="tenders")
        sent = _capture(client, monkeypatch)

        client.complete(system="s", user="x" * 5000, kind="tender")

        assert len(sent["user"]) == 500

    def test_a_transport_failure_returns_a_result_not_an_exception(
        self, configured, monkeypatch
    ):
        """Every failure looks the same to the caller, because the correct
        response is identical: keep the deterministic result."""
        client = configured(scope="tenders")

        def _boom(system, user, max_tokens):
            raise TimeoutError("too slow")

        monkeypatch.setattr(client, "_post", _boom)

        result = client.complete(system="s", user="x" * 100, kind="tender")

        assert result.ok is False
        assert result.reason == "TimeoutError"


class TestJsonParsingToleratesPackaging:
    def test_a_fenced_block_still_parses(self):
        """Models wrap JSON in fences even when told not to. Failing on that
        discards a correct answer over its packaging."""
        result = LlmResult(ok=True, content='```json\n{"technologies": ["Docker"]}\n```')

        assert result.as_json() == {"technologies": ["Docker"]}

    def test_prose_instead_of_json_yields_none(self):
        assert LlmResult(ok=True, content="Je ne peux pas.").as_json() is None

    def test_a_failed_call_yields_none(self):
        assert LlmResult(ok=False, reason="disabled").as_json() is None


class TestRefinementNeverLosesContent:
    def test_a_summary_is_rejected_and_the_original_kept(self, configured, monkeypatch):
        """Asked to clean, a model will sometimes summarise. The result reads
        beautifully and has quietly dropped the clause naming the required
        technology — a loss nobody would notice.
        """
        client = configured(scope="tenders")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        monkeypatch.setattr(client, "_post", lambda s, u, m: "Résumé très court.")

        original = "Le titulaire doit maîtriser Docker et Kubernetes. " * 20
        result = refine_ocr_text(original, kind="tender")

        assert result.changed is False
        assert result.reason == "looks_summarised"
        assert result.text == original

    def test_a_faithful_cleanup_is_accepted(self, configured, monkeypatch):
        client = configured(scope="tenders")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        original = "Le titulaire doit maitriser Docker. " * 20
        cleaned = original.replace("maitriser", "maîtriser")
        monkeypatch.setattr(client, "_post", lambda s, u, m: cleaned)

        result = refine_ocr_text(original, kind="tender")

        assert result.changed is True
        assert "maîtriser" in result.text

    def test_a_refusal_leaves_the_text_untouched(self, configured, monkeypatch):
        client = configured(scope="tenders")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)

        result = refine_ocr_text("x" * 400, kind="cv")

        assert result.changed is False
        assert result.text == "x" * 400

    def test_short_text_is_not_worth_a_call(self, configured, monkeypatch):
        client = configured(scope="tenders")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        sent = _capture(client, monkeypatch)

        refine_ocr_text("court", kind="tender")

        assert sent == {}


class TestStructuredRequirements:
    def test_a_well_formed_answer_is_normalised(self, configured, monkeypatch):
        client = configured(scope="tenders")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        monkeypatch.setattr(
            client,
            "_post",
            lambda s, u, m: '{"technologies": ["Docker", " "], "experience_min_annees": 5}',
        )

        parsed = structure_requirements("Le titulaire doit " + "x" * 100, kind="tender")

        assert parsed["technologies"] == ["Docker"]
        assert parsed["experience_min_annees"] == 5
        # Absent fields are present and empty, so no caller has to guard them.
        assert parsed["certifications"] == []

    def test_an_unavailable_model_returns_none_not_an_empty_structure(
        self, configured, monkeypatch
    ):
        """"The model was off" and "this passage requires nothing" are
        different facts: one is worth retrying, the other is not."""
        client = configured(key="")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)

        assert structure_requirements("Le titulaire doit " + "x" * 100) is None


class TestAModelAnsweringInTheWrongShape:
    """A model asked for strings returns objects often enough that discarding
    them would throw away correct answers over their packaging."""

    def test_an_object_yields_its_label(self, configured, monkeypatch):
        client = configured(scope="tenders")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        monkeypatch.setattr(
            client,
            "_post",
            lambda s, u, m: (
                '{"profils": [{"intitule": "Expert RGAA", "niveau": "avere"}, '
                '"Developpeur"]}'
            ),
        )

        parsed = structure_requirements("Le titulaire doit " + "x" * 100)

        assert parsed["profils"] == ["Expert RGAA", "Developpeur"]

    def test_a_sentence_is_not_a_label(self, configured, monkeypatch):
        """Anything longer than a line is an obligation, and belongs in
        `exigences` rather than as a badge on screen."""
        client = configured(scope="tenders")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        long_value = "x" * 200
        monkeypatch.setattr(
            client, "_post", lambda s, u, m: '{"technologies": ["' + long_value + '"]}'
        )

        parsed = structure_requirements("Le titulaire doit " + "y" * 100)

        assert parsed["technologies"] == []

    def test_duplicates_are_collapsed(self, configured, monkeypatch):
        client = configured(scope="tenders")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        monkeypatch.setattr(
            client, "_post", lambda s, u, m: '{"technologies": ["Docker", "Docker"]}'
        )

        parsed = structure_requirements("Le titulaire doit " + "x" * 100)

        assert parsed["technologies"] == ["Docker"]
