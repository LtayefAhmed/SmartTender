from datetime import date

import pytest

from app.services.cv_filter_evidence import evaluate_cv_filters


def test_all_requested_criteria_have_verbatim_evidence():
    text = "Profil : 7 ans d'expérience\nLangues : Français, Anglais C1\nCertifications\nPMP\nITIL Foundation\nFormation\nMaster en informatique"
    result = evaluate_cv_filters(text, {"min_experience_years": 5, "languages": ["French", "English"], "certifications": ["PMP", "ITIL"], "education": ["Licence", "Master"]})
    assert result["status"] == "pass"
    assert len(result["checks"]) == 6
    assert all(c["evidence"] and c["evidence"][0] in text for c in result["checks"])
    assert result["profile"]["experience_years"] == 7


def test_languages_and_certifications_are_all_of():
    result = evaluate_cv_filters("Languages: French\nCertifications: PMP", {"languages": ["Français", "Anglais"], "certifications": ["PMP", "ITIL"]})
    assert [c["status"] for c in result["checks"]] == ["pass", "unknown", "pass", "unknown"]
    assert result["status"] == "unknown"


@pytest.mark.parametrize("text", ["", "Python developer", "Worked at English Telecom.\nScrum Master\nSoftware engineer\nCompany ISO 27001 certified"])
def test_absence_and_job_titles_do_not_prove_qualifications(text):
    result = evaluate_cv_filters(text, {"languages": ["English"], "certifications": ["ISO 27001", "Scrum Master"], "education": ["Master", "Ingénieur"]})
    assert result["status"] == "unknown"
    assert all(c["status"] == "unknown" for c in result["checks"])


@pytest.mark.parametrize("text", ["Certifications: PMP in progress", "Certifications: PMP expired", "Certifications: not certified PMP", "Certifications: PMP training"])
def test_pending_expired_negative_and_training_are_not_passes(text):
    result = evaluate_cv_filters(text, {"certifications": ["PMP"]})
    assert result["status"] == "unknown"
    assert result["checks"][0]["evidence"] == [text]


def test_aliases_and_word_boundaries():
    result = evaluate_cv_filters("Certifications: Project Management Professional, AZ-900\nLanguages: English", {"certifications": ["PMP", "Azure Fundamentals", "CISA"], "languages": ["Anglais"]})
    assert [c["status"] for c in result["checks"]] == ["pass", "pass", "pass", "unknown"]


@pytest.mark.parametrize("text,expected", [("2 years of experience", "fail"), ("5 years of professional experience", "pass"), ("Expérience: 5 ans", "pass"), ("2+ years of experience", "unknown"), ("Python for 8 years", "unknown"), ("10 years of experience required", "unknown")])
def test_experience_threshold(text, expected):
    assert evaluate_cv_filters(text, {"min_experience_years": 5})["status"] == expected


def test_conflicting_experience_is_unverified():
    result = evaluate_cv_filters("5 years of experience\n3 years of experience", {"min_experience_years": 4})
    assert result["status"] == "unknown"
    assert len(result["checks"][0]["evidence"]) == 2


def test_employment_dates_merge_overlap_and_ignore_education():
    text = "Education\n01/2010 - 01/2016 Bachelor\nWork experience\n01/2020 - 01/2023 Developer\n01/2022 - 01/2024 Engineer"
    result = evaluate_cv_filters(text, {"min_experience_years": 4}, today=date(2026, 1, 1))
    assert result["status"] == "pass"
    assert result["checks"][0]["observed"] == 4
    assert len(result["checks"][0]["evidence"]) == 2


def test_year_only_dates_are_conservative_and_incomplete_history_does_not_fail():
    result = evaluate_cv_filters("Experience\n2020 - 2024 Engineer", {"min_experience_years": 4}, today=date(2026, 1, 1))
    assert result["status"] == "unknown"
    assert result["checks"][0]["observed"] == 3.08


def test_current_job_and_future_dates():
    text = "Work experience\n01/2020 - present Developer\n01/2030 - 01/2035 Director"
    result = evaluate_cv_filters(text, {"min_experience_years": 6}, today=date(2026, 1, 1))
    assert result["status"] == "pass"
    assert result["profile"]["experience_years"] == 6


def test_no_filters_preserves_eligibility():
    assert evaluate_cv_filters("", {})["status"] == "pass"


@pytest.mark.parametrize("text,expected,age", [("Age: 32", "pass", 32), ("Age: 20", "fail", 20), ("32 years old", "pass", 32), ("32 years of experience", "unknown", None), ("Date de naissance: 24/09/1994", "pass", 31), ("DOB: 1994-09-23", "pass", 32), ("Born: 1994", "unknown", None), ("DOB: 31/02/1994", "unknown", None), ("Age: 30\nAge: 40", "unknown", None)])
def test_age_requires_explicit_evidence(text, expected, age):
    result = evaluate_cv_filters(text, {"age_min": 25, "age_max": 35}, today=date(2026, 9, 23))
    assert result["status"] == expected
    assert result["profile"]["age"] == age
    assert result["checks"][0]["criterion"] == "age"


def test_french_and_english_month_names():
    result = evaluate_cv_filters("Expérience professionnelle\nJanvier 2020 - December 2024 Développeur", {"min_experience_years": 4}, today=date(2026, 1, 1))
    assert result["status"] == "pass"
    assert result["checks"][0]["observed"] == 4.92


@pytest.mark.parametrize("text", ["Certifications\nPMP\nExpired", "Certifications\nPMP\nPMP expired", "Education\nMaster\nExpected 2027"])
def test_pending_on_next_line_or_conflicting_credential_is_not_confirmed(text):
    filters = {"education": ["Master"]} if "Education" in text else {"certifications": ["PMP"]}
    assert evaluate_cv_filters(text, filters)["status"] == "unknown"
