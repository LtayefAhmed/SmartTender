"""Searching the CV base directly, and reading criteria out of a CV.

Two rules separate this from tender matching, and both come from a measurement.

**Chosen is not inferred.** A tender's technology list is read out of a
document — twenty-four terms where matching one is chance. A search's list is
ticked by a recruiter, who meant all of it. So here every technology is
required, and there is no scaling floor.

**Silence is not absence.** Over 344 CVs, 21% state a language and 10% a
recognised certification. Filtering hard on those would reject a candidate for
not having written something down, so they rank rather than exclude.
"""

from __future__ import annotations

import pytest

from app.services.cv_criteria import extract_criteria, normalise_language
from app.services.matching import required_technologies
from app.services.profile_search import ProfileQuery, SearchWeights, _combine


class TestCriteriaAreReadConservatively:
    """Every rule here demands its context, because the failures are silent:
    a pattern that fires too readily produces a plausible wrong answer."""

    def test_a_degree_is_recognised(self):
        assert extract_criteria("Education Master of Science : Networks").education_level == 5
        assert extract_criteria("Bachelor of Science in CyberSecurity").education_level == 3
        assert extract_criteria("BTS Services Informatiques").education_level == 2
        assert extract_criteria("Doctorat en informatique").education_level == 8

    def test_a_contract_clause_is_not_a_degree(self):
        """Measured on a real CV: "developing **Master** Service Agreements"
        is a contract, and a bare-token rule called it a Bac+5."""
        criteria = extract_criteria("Developing Master Service Agreements (MSA)")

        assert criteria.education_level is None

    def test_a_job_title_is_not_a_diploma(self):
        """"Ingénieur système" is what someone does, not what they hold."""
        assert extract_criteria("Ingénieur système réseau, 5 ans").education_level is None
        assert extract_criteria("Diplôme d'ingénieur en informatique").education_level == 5

    def test_the_highest_level_wins(self):
        """A CV lists its degrees oldest-first as often as newest-first, and a
        recruiter filters on the ceiling."""
        criteria = extract_criteria("Licence en informatique 2018. Master of Science 2020.")

        assert criteria.education_level == 5

    def test_languages_are_canonicalised(self):
        criteria = extract_criteria("Langues : Anglais (4/5), français courant, English")

        assert set(criteria.languages) == {"anglais", "français"}

    @pytest.mark.parametrize(
        ("spelling", "canonical"),
        [("English", "anglais"), ("ANGLAIS", "anglais"), ("Arabic", "arabe")],
    )
    def test_a_spelling_maps_to_one_token(self, spelling, canonical):
        assert normalise_language(spelling) == canonical

    def test_an_unknown_language_is_not_invented(self):
        assert normalise_language("klingon") is None

    def test_a_certified_copy_is_not_a_certification(self):
        """"Certified copy of the diploma" is a document. A loose rule that
        looked for any word near "certified" collected those."""
        criteria = extract_criteria("Certified true copy of the diploma attached")

        assert criteria.certifications == []

    def test_a_real_certification_is_caught(self):
        criteria = extract_criteria("AWS Certified Solutions Architect, ITIL v4, TOEIC 850")

        assert set(criteria.certifications) >= {"AWS Certified", "ITIL", "TOEIC"}


class TestAmbiguousTokensNeedTheirContext:
    """Word boundaries stop `.NET` firing inside "Khazanet". They do not stop
    `Java` firing inside "Central Java Inter-Mission School", which put an
    English teacher on a Java shortlist."""

    def test_a_place_name_is_not_a_language(self):
        assert required_technologies("attended Central Java Inter-Mission School") == []

    def test_the_language_is_still_found(self):
        assert "Java" in required_technologies("Développement Java 17 et Spring Boot")

    def test_mentioning_both_keeps_the_skill(self):
        """The first fix searched a 40-character window and produced a worse
        bug than the one it cured: a genuine Java developer who mentioned
        growing up in Central Java lost the skill, poisoned from two sentences
        away. A false negative on a real competence costs more than a false
        positive a human dismisses on sight."""
        found = required_technologies("Grew up in Central Java. Now a Java 17 developer.")

        assert "Java" in found

    def test_a_french_table_is_not_a_bi_tool(self):
        assert required_technologies("Voir le tableau de bord et le tableau comparatif") == []

    def test_the_bi_tool_is_still_found(self):
        assert "Tableau" in required_technologies("Reporting avec Tableau Desktop")

    def test_an_english_verb_is_not_a_language(self):
        """The bare token "Go" is absent from the lexicon entirely — no
        disqualifying pattern covers "go further", "go live", "go beyond".
        Same remedy as "SAGE" in the scoring profile: drop the ambiguous
        token, keep the unmistakable one."""
        assert required_technologies("I go to work and go further every day") == []
        assert "Golang" in required_technologies("Backend written in Golang")


class TestTheQueryIsBuiltFromEverythingAsked:
    def test_filters_become_part_of_the_query_text(self):
        """A filters-only search still has to *rank* what passes, and the order
        should follow how central those skills are to a CV."""
        text = ProfileQuery(technologies=["Java", "Spring"], languages=["anglais"]).as_query_text()

        assert "Java" in text and "Spring" in text and "anglais" in text

    def test_an_empty_query_is_recognised(self):
        assert ProfileQuery().is_empty() is True
        assert ProfileQuery(technologies=["Java"]).is_empty() is False


class TestUnaskedCriteriaDoNotCapTheScore:
    def test_a_technologies_only_search_can_reach_one(self):
        """Leaving an unused weight in the denominator would cap the score at
        0.55 and make every result look mediocre against a scale the recruiter
        never chose."""
        score, _ = _combine(
            weights=SearchWeights(),
            query=ProfileQuery(technologies=["Java"]),
            similarity=1.0,
            language_ratio=0.0,
            certification_ratio=0.0,
            meets_education=True,
        )

        assert score == pytest.approx(1.0)

    def test_an_asked_criterion_pulls_the_score_down_when_unmet(self):
        asked = ProfileQuery(technologies=["Java"], languages=["anglais"])
        with_language, _ = _combine(
            weights=SearchWeights(), query=asked, similarity=1.0,
            language_ratio=1.0, certification_ratio=0.0, meets_education=True,
        )
        without_language, _ = _combine(
            weights=SearchWeights(), query=asked, similarity=1.0,
            language_ratio=0.0, certification_ratio=0.0, meets_education=True,
        )

        assert with_language > without_language
        # But never to zero: an unstated language is a silence, not a refusal.
        assert without_language > 0.5

    def test_the_weights_are_versioned(self):
        assert SearchWeights().version
