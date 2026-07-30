"""Scoring engine: weighting, bands, explainability and graceful degradation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.connectors.models import NormalizedTender
from app.core.enums import RelevanceBand
from app.core.identity import utc_now
from app.services.scoring import (
    CriterionScorer,
    ScoringEngine,
    register_scorer,
)


@pytest.fixture()
def engine() -> ScoringEngine:
    return ScoringEngine()


class TestOverallScoring:
    def test_an_on_target_it_tender_scores_highly(self, engine, make_tender):
        result = engine.score(make_tender())
        assert result.score > 0.7
        assert result.band in (RelevanceBand.HIGHLY_RELEVANT, RelevanceBand.RELEVANT)

    def test_a_blocking_keyword_vetoes_the_tender(self, engine, make_tender):
        tender = make_tender(
            title="Travaux de génie civil pour la réhabilitation d'un entrepôt",
            description="Démolition partielle, gros œuvre et charpente métallique.",
            sector="Bâtiment et travaux publics",
        )
        result = engine.score(tender)
        assert result.band is RelevanceBand.OUT_OF_SCOPE
        assert result.score == 0.0
        assert "génie civil" in result.veto_reason

    def test_score_is_not_binary(self, engine, make_tender):
        """Three tenders of decreasing fit must produce three distinct scores."""
        strong = engine.score(make_tender()).score
        medium = engine.score(
            make_tender(
                title="Formation des utilisateurs aux outils bureautiques",
                description="Sessions de formation à destination des agents.",
                sector="Formation",
                cpv_codes=[],
            )
        ).score
        weak = engine.score(
            make_tender(
                title="Acquisition de mobilier de bureau",
                description="Fourniture de chaises et de bureaux.",
                sector="Fournitures",
                cpv_codes=["39100000"],
                buyer="Commune de Sfax",
                estimated_budget=Decimal("20000"),
            )
        ).score
        assert strong > medium > weak

    def test_every_score_is_bounded(self, engine, make_tender):
        for tender in (make_tender(), make_tender(title="x" * 500)):
            assert 0.0 <= engine.score(tender).score <= 1.0


class TestDeadlineCriterion:
    def _score(self, engine, make_tender, days):
        tender = make_tender(deadline=utc_now() + timedelta(days=days))
        return engine.score(tender).breakdown["deadline_proximity"]["value"]

    def test_the_curve_peaks_in_the_workable_window(self, engine, make_tender):
        assert self._score(engine, make_tender, 25) == 1.0

    def test_an_imminent_deadline_is_nearly_worthless(self, engine, make_tender):
        # Technically open, practically unbiddable.
        assert self._score(engine, make_tender, 2) < 0.2

    def test_a_distant_deadline_decays(self, engine, make_tender):
        assert self._score(engine, make_tender, 120) < 1.0

    def test_an_expired_deadline_scores_zero(self, engine, make_tender):
        assert self._score(engine, make_tender, -5) == 0.0

    def test_a_missing_deadline_is_neutral_not_zero(self, engine, make_tender):
        value = engine.score(make_tender(deadline=None)).breakdown["deadline_proximity"]["value"]
        assert 0.2 < value < 0.6


class TestMissingDataIsNeutral:
    def test_an_unpublished_budget_does_not_penalise(self, engine, make_tender):
        """A portal that never publishes budgets must not have all its tenders
        permanently down-ranked for that editorial habit."""
        with_budget = engine.score(make_tender(estimated_budget=Decimal("850000")))
        without = engine.score(make_tender(estimated_budget=None, currency=None))
        assert abs(with_budget.score - without.score) < 0.12

    def test_a_zeroed_criterion_would_have_been_much_worse(self, engine, make_tender):
        without = engine.score(make_tender(estimated_budget=None, currency=None))
        breakdown = without.breakdown["budget"]
        assert breakdown["value"] == pytest.approx(0.5, abs=0.01)
        assert breakdown["applicable"] is True


class TestExplainability:
    def test_every_weighted_criterion_is_explained(self, engine, make_tender):
        result = engine.score(make_tender())
        for name, weight in engine.weights.items():
            assert name in result.breakdown, f"criterion '{name}' produced no breakdown"
            entry = result.breakdown[name]
            assert entry["explanation"]
            assert entry["weight"] == weight

    def test_matched_keywords_are_named(self, engine, make_tender):
        result = engine.score(make_tender())
        matched = result.breakdown["keywords"]["details"]["matched"]
        assert matched
        assert result.breakdown["keywords"]["explanation"].startswith("Matched")

    def test_the_matching_field_of_work_is_named(self, engine, make_tender):
        result = engine.score(make_tender())
        assert result.breakdown["field_of_work"]["details"]["profile"]

    def test_weights_are_snapshotted_with_the_result(self, engine, make_tender):
        result = engine.score(make_tender())
        assert result.weights == engine.weights
        assert result.profile_version == engine.version


class TestBands:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0.95, RelevanceBand.HIGHLY_RELEVANT),
            (0.75, RelevanceBand.HIGHLY_RELEVANT),
            (0.60, RelevanceBand.RELEVANT),
            (0.50, RelevanceBand.RELEVANT),
            (0.20, RelevanceBand.LOW_RELEVANCE),
        ],
    )
    def test_thresholds(self, engine, score, expected):
        assert engine.band_for(score) is expected

    def test_band_metadata_carries_labels_and_colours(self, engine):
        metadata = engine.band_metadata()
        assert metadata["highly_relevant"]["label"] == "Highly Relevant"
        assert metadata["highly_relevant"]["color"].startswith("#")


class TestConfigurability:
    def test_weights_come_from_configuration(self, make_tender):
        """Re-weighting is a config change with a visible effect, not a code change."""
        profile = {
            "name": "deadline-only",
            "version": "test-1",
            "weights": {"deadline_proximity": 1.0},
            "bands": {
                "highly_relevant": {"min_score": 0.75},
                "relevant": {"min_score": 0.5},
                "low_relevance": {"min_score": 0.0},
            },
            "criteria": {
                "deadline_proximity": {
                    "min_viable_days": 5,
                    "ideal_min_days": 12,
                    "ideal_max_days": 45,
                    "horizon_days": 180,
                }
            },
        }
        result = ScoringEngine(profile).score(make_tender())
        assert result.score == 1.0
        assert set(result.breakdown) == {"deadline_proximity"}
        assert result.profile_version == "test-1"

    def test_a_zero_weight_disables_a_criterion(self, make_tender):
        profile = {
            "name": "t",
            "version": "t",
            "weights": {"keywords": 1.0, "budget": 0.0},
            "bands": {"relevant": {"min_score": 0.5}, "low_relevance": {"min_score": 0.0}},
            "criteria": {"keywords": {"include": {"développement": 1.0}, "saturation_at": 1}},
        }
        result = ScoringEngine(profile).score(make_tender())
        assert "budget" not in result.breakdown


class TestGracefulDegradation:
    def test_a_failing_criterion_does_not_leave_the_tender_unscored(self, make_tender):
        class ExplodingScorer(CriterionScorer):
            name = "exploding"

            def score(self, tender, config, context):
                raise RuntimeError("scorer bug")

        register_scorer(ExplodingScorer())
        profile = {
            "name": "t",
            "version": "t",
            "weights": {"exploding": 0.5, "keywords": 0.5},
            "bands": {"relevant": {"min_score": 0.5}, "low_relevance": {"min_score": 0.0}},
            "criteria": {"keywords": {"include": {"développement": 1.0}, "saturation_at": 1}},
        }
        result = ScoringEngine(profile).score(make_tender())

        # The broken criterion is recorded, the rest still produced a score.
        assert result.breakdown["exploding"]["error"] == "RuntimeError"
        assert result.score > 0

    def test_an_unknown_criterion_is_skipped(self, make_tender):
        profile = {
            "name": "t",
            "version": "t",
            "weights": {"does_not_exist": 1.0, "keywords": 1.0},
            "bands": {"relevant": {"min_score": 0.5}, "low_relevance": {"min_score": 0.0}},
            "criteria": {"keywords": {"include": {"développement": 1.0}, "saturation_at": 1}},
        }
        result = ScoringEngine(profile).score(make_tender())
        assert "does_not_exist" not in result.breakdown

    def test_a_minimal_tender_still_scores(self, engine):
        bare = NormalizedTender(connector_key="manual", title="Avis de marché")
        result = engine.score(bare)
        assert 0.0 <= result.score <= 1.0


class TestCpvCriterion:
    def test_exact_family_beats_a_distant_one(self, make_tender):
        profile = {
            "name": "t",
            "version": "t",
            "weights": {"cpv_similarity": 1.0},
            "bands": {"relevant": {"min_score": 0.5}, "low_relevance": {"min_score": 0.0}},
            "criteria": {"cpv_similarity": {"preferred_codes": ["72200000"]}},
        }
        engine = ScoringEngine(profile)
        exact = engine.score(make_tender(cpv_codes=["72200000"])).score
        related = engine.score(make_tender(cpv_codes=["72500000"])).score
        unrelated = engine.score(make_tender(cpv_codes=["45000000"])).score
        assert exact == 1.0
        assert exact > related > unrelated
