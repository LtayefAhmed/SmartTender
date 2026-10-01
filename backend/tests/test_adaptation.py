"""Reformulating a CV without inventing anything.

Asking a model not to invent is a hope. These tests are about the mechanism
that makes it a property: every reformulated passage is checked back against
its source, and one that names a technology or a year the source did not is
discarded with the original kept.

The model is stubbed throughout. That is the point rather than a shortcut —
what is under test is the *verification*, and a suite that called a paid API
would be a suite people switch off at the first quota.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.adaptation import Adaptation, adapt_experiences


def _experience(**overrides: Any) -> dict[str, Any]:
    base = {
        "debut": "2019",
        "fin": "2022",
        "poste": "Développeur Java",
        "employeur": "",
        "lieu": "",
        "missions": "Migration du socle applicatif Java et Spring vers Docker.",
    }
    base.update(overrides)
    return base


class _Reply:
    """A stubbed LLM answer."""

    def __init__(self, payload: Any, ok: bool = True, reason: str | None = None):
        self.ok = ok
        self.reason = reason
        self._payload = payload
        self.redactions = {"NOM": 1}
        self.content = "stub"

    def as_json(self) -> Any:
        return self._payload


@pytest.fixture
def llm(monkeypatch):
    """Install a scripted model and record what it was asked."""
    box: dict[str, Any] = {}

    def install(payload: Any, ok: bool = True, reason: str | None = None) -> None:
        class _Client:
            def complete(self, **kwargs):
                box["prompt"] = kwargs.get("user", "")
                box["kind"] = kwargs.get("kind")
                return _Reply(payload, ok=ok, reason=reason)

        monkeypatch.setattr("app.services.llm.get_llm", lambda: _Client())

    box["install"] = install
    return box


class TestTheGuardCatchesAnInventedCompetence:
    def test_a_technology_absent_from_the_source_is_refused(self, llm):
        """The failure this exists for: the model writes Kubernetes over a role
        that never mentioned it, and a bid manager submits a CV claiming an
        orchestration skill the consultant will be interviewed on."""
        llm["install"](
            {
                "experiences": [
                    {
                        "index": 0,
                        "pertinence": 90,
                        "missions": "Conception Java et Spring, orchestration Kubernetes.",
                    }
                ]
            }
        )

        result = adapt_experiences([_experience()])

        assert result.rejected == 1
        assert result.reformulated == 0
        assert result.invented_terms == ["Kubernetes"]
        # The original survives untouched — a refused reformulation must not
        # cost the document its content.
        assert "Migration du socle applicatif" in result.experiences[0]["missions"]

    def test_an_invented_year_is_refused(self, llm):
        """Dates in a funder's CV form are checked against the expert's record."""
        llm["install"](
            {
                "experiences": [
                    {"index": 0, "pertinence": 80, "missions": "Depuis 2015, socle Java."}
                ]
            }
        )

        result = adapt_experiences([_experience()])

        assert result.rejected == 1
        assert "2015" not in result.experiences[0]["missions"]

    def test_a_year_already_in_the_source_is_allowed(self, llm):
        llm["install"](
            {
                "experiences": [
                    {
                        "index": 0,
                        "pertinence": 80,
                        "missions": "De 2019 à 2022, migration du socle Java.",
                    }
                ]
            }
        )

        result = adapt_experiences([_experience()])

        assert result.reformulated == 1

    def test_an_honest_reformulation_is_kept(self, llm):
        """The pass has to be worth running: a rewording that stays inside the
        source's facts must survive the guard."""
        llm["install"](
            {
                "experiences": [
                    {
                        "index": 0,
                        "pertinence": 95,
                        "missions": (
                            "Conception et migration d'applications "
                            "transactionnelles Java / Spring, conteneurisées Docker."
                        ),
                    }
                ]
            }
        )

        result = adapt_experiences([_experience()])

        assert result.reformulated == 1
        assert result.rejected == 0
        assert "transactionnelles" in result.experiences[0]["missions"]

    def test_a_reformulation_that_balloons_is_refused(self, llm):
        llm["install"](
            {"experiences": [{"index": 0, "pertinence": 50, "missions": "Java. " * 300}]}
        )

        result = adapt_experiences([_experience()])

        assert result.rejected == 1


class TestSelectionReordersWithoutDropping:
    def test_relevance_decides_the_order(self, llm):
        llm["install"](
            {
                "experiences": [
                    {"index": 0, "pertinence": 20, "missions": "Support applicatif."},
                    {"index": 1, "pertinence": 95, "missions": "Migration Java."},
                ]
            }
        )
        roles = [
            _experience(poste="Support", missions="Support applicatif de niveau 2."),
            _experience(poste="Développeur", missions="Migration Java du socle."),
        ]

        result = adapt_experiences(roles)

        assert result.experiences[0]["poste"] == "Développeur"

    def test_a_role_the_model_ignored_is_not_lost(self, llm):
        """Silence from a model is not a decision to delete somebody's job."""
        llm["install"](
            {"experiences": [{"index": 0, "pertinence": 90, "missions": "Migration Java."}]}
        )
        roles = [_experience(poste="Développeur"), _experience(poste="Analyste")]

        result = adapt_experiences(roles)

        assert {item["poste"] for item in result.experiences} == {"Développeur", "Analyste"}

    def test_a_duplicated_index_does_not_duplicate_a_job(self, llm):
        llm["install"](
            {
                "experiences": [
                    {"index": 0, "pertinence": 90, "missions": "Migration Java."},
                    {"index": 0, "pertinence": 40, "missions": "Autre chose."},
                ]
            }
        )

        result = adapt_experiences([_experience()])

        assert len(result.experiences) == 1

    def test_an_out_of_range_index_is_ignored(self, llm):
        llm["install"](
            {"experiences": [{"index": 7, "pertinence": 90, "missions": "Inventé."}]}
        )

        result = adapt_experiences([_experience()])

        assert result.reformulated == 0
        assert len(result.experiences) == 1


class TestFailureIsAlwaysSafe:
    """No key, a timeout, a refusal, malformed JSON. The correct response is
    identical every time: keep the deterministic text and produce the
    document. The pass is an improvement, never a dependency."""

    @pytest.mark.parametrize(
        ("payload", "ok", "reason", "status"),
        [
            (None, False, "disabled", "unavailable"),
            (None, False, "out_of_scope:cv", "unavailable"),
            (None, False, "TimeoutException", "unavailable"),
            (None, True, None, "unusable"),
            ({"experiences": []}, True, None, "unusable"),
            ({"autre_chose": 1}, True, None, "unusable"),
        ],
    )
    def test_every_failure_keeps_the_original(self, llm, payload, ok, reason, status):
        llm["install"](payload, ok=ok, reason=reason)

        result = adapt_experiences([_experience()])

        assert result.status == status
        assert result.llm_used is False
        assert "Migration du socle applicatif" in result.experiences[0]["missions"]

    def test_no_experience_asks_the_model_nothing(self, llm):
        llm["install"]({"experiences": [{"index": 0, "pertinence": 1, "missions": "x"}]})

        result = adapt_experiences([])

        assert result.status == "no_input"
        assert "prompt" not in llm


class TestWhatIsSentAndWhatComesBack:
    def test_the_cv_scope_is_declared(self, llm):
        """`kind` decides whether the sovereignty boundary lets this leave at
        all, and an unknown kind fails closed. Sending CV passages under
        `tender` would slip personal data past the switch."""
        llm["install"]({"experiences": [{"index": 0, "pertinence": 9, "missions": "Java."}]})

        adapt_experiences([_experience()])

        assert llm["kind"] == "cv"

    def test_the_requirements_reach_the_prompt(self, llm):
        llm["install"]({"experiences": [{"index": 0, "pertinence": 9, "missions": "Java."}]})

        adapt_experiences([_experience()], requirements=["Tierce maintenance Java/Spring"])

        assert "Tierce maintenance Java/Spring" in llm["prompt"]

    def test_past_responses_are_offered_as_style_not_facts(self, llm):
        """The seam for the validated-response referential. Borrowing a *fact*
        from someone else's past response would be inventing one here, so the
        prompt says so explicitly."""
        llm["install"]({"experiences": [{"index": 0, "pertinence": 9, "missions": "Java."}]})

        adapt_experiences([_experience()], past_responses=["Notre équipe a livré…"])

        assert "ne pas copier les faits" in llm["prompt"]

    def test_the_report_says_whether_a_model_touched_it(self, llm):
        llm["install"](
            {"experiences": [{"index": 0, "pertinence": 90, "missions": "Migration Java."}]}
        )

        report = adapt_experiences([_experience()]).to_dict()

        assert report["llm_used"] is True
        assert set(report) == {
            "status",
            "llm_used",
            "reformulated",
            "rejected",
            "invented_terms",
            "flagged",
        }

    def test_an_empty_result_serialises(self):
        assert Adaptation(experiences=[], status="no_input").to_dict()["llm_used"] is False


class TestTheSemanticNetClaimsOnlyWhatItWasMeasuredToHold:
    """Built first as a fidelity gate at 0.65, it rejected eight good passages
    out of eight: comparing a long source to a short reformulation measures
    compression, and dropping detail is allowed. Measured per sentence it still
    does not separate cleanly — 0.064 between an invented claim and a faithful
    compression — so it was split into an absurdity floor and a flag."""

    def test_a_sentence_supported_by_nothing_is_refused(self, llm):
        llm["install"](
            {
                "experiences": [
                    {
                        "index": 0,
                        "pertinence": 80,
                        "missions": "Conduite d'audits comptables et états financiers.",
                    }
                ]
            }
        )

        result = adapt_experiences([_experience()], similarity=lambda a, b: 0.10)

        assert result.rejected == 1
        assert "Migration du socle applicatif" in result.experiences[0]["missions"]

    def test_a_faithful_compression_is_not_refused(self, llm):
        """The failure the first version produced: a correct summary scoring
        low simply because it is shorter than its source."""
        llm["install"](
            {
                "experiences": [
                    {
                        "index": 0,
                        "pertinence": 80,
                        "missions": "Migration du socle applicatif Java vers Docker.",
                    }
                ]
            }
        )

        result = adapt_experiences([_experience()], similarity=lambda a, b: 0.30)

        assert result.rejected == 0
        assert result.reformulated == 1

    def test_a_thin_sentence_is_kept_and_named(self, llm):
        """Not provably wrong, so not discarded — but the reviewer reads it
        first. The platform naming its own weak spots beats a document that
        looks uniformly confident."""
        llm["install"](
            {
                "experiences": [
                    {
                        "index": 0,
                        "pertinence": 80,
                        "missions": "Migration du socle applicatif Java vers Docker.",
                    }
                ]
            }
        )

        result = adapt_experiences([_experience()], similarity=lambda a, b: 0.30)

        assert result.flagged
        assert "Migration du socle" in result.flagged[0]

    def test_a_well_supported_sentence_is_not_flagged(self, llm):
        llm["install"](
            {
                "experiences": [
                    {
                        "index": 0,
                        "pertinence": 80,
                        "missions": "Migration du socle applicatif Java vers Docker.",
                    }
                ]
            }
        )

        result = adapt_experiences([_experience()], similarity=lambda a, b: 0.80)

        assert result.flagged == []

    def test_an_encoder_failure_does_not_block_the_document(self, llm):
        """A model that cannot load must not cost a bid its dossier. The
        lexical guards already ran; this net abstains."""

        def _explode(a, b):
            raise RuntimeError("encoder unavailable")

        llm["install"](
            {
                "experiences": [
                    {"index": 0, "pertinence": 80, "missions": "Migration Java du socle."}
                ]
            }
        )

        result = adapt_experiences([_experience()], similarity=_explode)

        assert result.reformulated == 1
        assert result.rejected == 0
