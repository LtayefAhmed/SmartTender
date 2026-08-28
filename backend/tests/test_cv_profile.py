"""Structuring a CV's own facts, and applying a recruiter's filters to them.

Same infra-free, mocked-LLM style as ``test_llm.py``'s
``TestStructuredRequirements`` — no network, no database, no 470 MB model.
"""

from __future__ import annotations

import pytest

from app.core.config import reset_settings_cache
from app.services.cv_profile import JobMatchFilters, apply_filters, structure_cv_profile
from app.services.llm import MistralClient


@pytest.fixture()
def configured(monkeypatch):
    """A client with a key, scoped to CVs, and no network."""

    def _make(scope: str = "tenders_and_cvs", key: str = "test-key"):
        monkeypatch.setenv("SMARTTENDER_LLM__MISTRAL_API_KEY", key)
        monkeypatch.setenv("SMARTTENDER_LLM__SCOPE", scope)
        reset_settings_cache()
        return MistralClient()

    yield _make
    reset_settings_cache()


def _capture(client, monkeypatch, reply: str = "{}"):
    sent: dict[str, str] = {}

    def _post(system, user, max_tokens):
        sent["system"] = system
        sent["user"] = user
        return reply

    monkeypatch.setattr(client, "_post", _post)
    return sent


class TestStructureCvProfile:
    def test_a_well_formed_answer_is_normalised(self, configured, monkeypatch):
        client = configured()
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        monkeypatch.setattr(
            client,
            "_post",
            lambda s, u, m: (
                '{"age": 34, "experience_years": 9, "education": "Ingenieur", '
                '"certifications": ["PMP", " "], "languages": ["Francais"], '
                '"skills": ["Docker"]}'
            ),
        )

        parsed = structure_cv_profile("Profil confirme. " + "x" * 100)

        assert parsed["age"] == 34
        assert parsed["experience_years"] == 9
        assert parsed["education"] == "Ingenieur"
        assert parsed["certifications"] == ["PMP"]
        assert parsed["languages"] == ["Francais"]
        assert parsed["skills"] == ["Docker"]

    def test_an_unavailable_model_returns_none_not_an_empty_structure(
        self, configured, monkeypatch
    ):
        client = configured(key="")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)

        assert structure_cv_profile("Profil confirme. " + "x" * 100) is None

    def test_short_text_is_not_worth_a_call(self, configured, monkeypatch):
        client = configured()
        sent = _capture(client, monkeypatch)
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)

        assert structure_cv_profile("court") is None
        assert sent == {}

    def test_an_implausible_age_is_discarded_not_trusted(self, configured, monkeypatch):
        client = configured()
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        monkeypatch.setattr(client, "_post", lambda s, u, m: '{"age": 400}')

        parsed = structure_cv_profile("Profil confirme. " + "x" * 100)

        assert parsed["age"] is None

    def test_an_implausible_experience_is_discarded_not_trusted(self, configured, monkeypatch):
        client = configured()
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        monkeypatch.setattr(client, "_post", lambda s, u, m: '{"experience_years": 120}')

        parsed = structure_cv_profile("Profil confirme. " + "x" * 100)

        assert parsed["experience_years"] is None

    def test_an_object_where_a_string_was_asked_yields_its_label(self, configured, monkeypatch):
        client = configured()
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        monkeypatch.setattr(
            client,
            "_post",
            lambda s, u, m: '{"certifications": [{"nom": "AWS Certified"}, "PMP"]}',
        )

        parsed = structure_cv_profile("Profil confirme. " + "x" * 100)

        assert parsed["certifications"] == ["AWS Certified", "PMP"]

    def test_a_cv_is_refused_when_the_scope_is_tenders_only(self, configured, monkeypatch):
        client = configured(scope="tenders")
        monkeypatch.setattr("app.services.llm.get_llm", lambda: client)
        sent = _capture(client, monkeypatch)

        assert structure_cv_profile("Profil confirme. " + "x" * 100) is None
        assert sent == {}


class TestApplyFilters:
    _FULL_PROFILE = {
        "age": 30,
        "experience_years": 6,
        "education": "Master",
        "certifications": ["PMP", "Prince2"],
        "languages": ["Francais", "Anglais"],
        "skills": ["Docker", "Kubernetes"],
    }

    def test_no_filters_always_passes(self):
        passed, reason = apply_filters(self._FULL_PROFILE, JobMatchFilters())
        assert passed is True
        assert reason is None

    def test_missing_data_never_rejects(self):
        empty_profile = {
            "age": None,
            "experience_years": None,
            "education": None,
            "certifications": [],
            "languages": [],
            "skills": [],
        }
        passed, reason = apply_filters(
            empty_profile,
            JobMatchFilters(age_min=25, min_experience_years=5, certifications=["PMP"]),
        )
        assert passed is True
        assert reason is None

    def test_age_below_minimum_is_rejected_with_a_reason(self):
        passed, reason = apply_filters(self._FULL_PROFILE, JobMatchFilters(age_min=35))
        assert passed is False
        assert "age" in reason

    def test_age_above_maximum_is_rejected(self):
        passed, _reason = apply_filters(self._FULL_PROFILE, JobMatchFilters(age_max=25))
        assert passed is False

    def test_insufficient_experience_is_rejected(self):
        passed, reason = apply_filters(
            self._FULL_PROFILE, JobMatchFilters(min_experience_years=10)
        )
        assert passed is False
        assert "experience" in reason

    def test_any_of_several_certifications_passes(self):
        passed, _ = apply_filters(
            self._FULL_PROFILE, JobMatchFilters(certifications=["ITIL", "PMP"])
        )
        assert passed is True

    def test_none_of_the_listed_certifications_is_rejected(self):
        passed, reason = apply_filters(
            self._FULL_PROFILE, JobMatchFilters(certifications=["ITIL", "Scrum Master"])
        )
        assert passed is False
        assert "certification" in reason

    def test_language_matching_is_accent_and_case_insensitive(self):
        passed, _ = apply_filters(self._FULL_PROFILE, JobMatchFilters(languages=["FRANCAIS"]))
        assert passed is True

    def test_a_candidate_passing_every_filter_passes(self):
        passed, reason = apply_filters(
            self._FULL_PROFILE,
            JobMatchFilters(
                age_min=25,
                age_max=40,
                min_experience_years=5,
                certifications=["PMP"],
                languages=["Anglais"],
                technologies=["Docker"],
            ),
        )
        assert passed is True
        assert reason is None
