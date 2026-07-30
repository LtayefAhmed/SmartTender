"""Relevance scoring — weighted, configurable, and explainable.

The score is **not binary**. Each criterion independently returns a value in
[0, 1] plus a sentence explaining it, and the engine combines them using
weights that live entirely in ``config/scoring.yaml``. Nothing is hardcoded:
adding a criterion is registering a scorer and giving it a weight.

Three design decisions carry most of the value:

**Normalise over what was actually scored.** A criterion that has no data
(unknown budget, missing CPV codes) is excluded from the denominator instead of
counted as zero. Otherwise every tender from a portal that does not publish
budgets would be permanently penalised for the portal's editorial habits.

**Blocking keywords are a veto, not a weight.** A civil-engineering tender is
not "slightly relevant" to a software integrator; it is out of scope. Those go
to a dedicated band so they can be excluded from the dashboard without being
confused with genuinely low-scoring IT work.

**Every score is explainable and reproducible.** The breakdown and the exact
weights are persisted with the result, so a ranking can be justified — and
recomputed identically — months later, even after the weights have changed.

A criterion that raises is logged and skipped; the tender still gets a score
from the others. Losing one signal must never mean losing the opportunity.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.connectors.models import NormalizedTender
from app.core.config import load_yaml_config
from app.core.enums import ProcurementType, RelevanceBand
from app.core.identity import as_utc, normalize_text, utc_now
from app.core.logging import get_logger
from app.core.metrics import scoring_runs_total, tender_score
from app.services.similarity import get_similarity_backend

logger = get_logger(__name__)

__all__ = [
    "CriterionResult",
    "CriterionScorer",
    "ScoreResult",
    "ScoringEngine",
    "get_scoring_engine",
    "register_scorer",
    "reset_scoring_engine",
]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CriterionResult:
    """One criterion's contribution."""

    value: float | None
    explanation: str
    details: dict[str, Any] = field(default_factory=dict)
    #: Set by a criterion that disqualifies the tender outright.
    veto: bool = False

    @property
    def applicable(self) -> bool:
        return self.value is not None


@dataclass(slots=True)
class ScoreResult:
    score: float
    band: RelevanceBand
    breakdown: dict[str, dict[str, Any]]
    weights: dict[str, float]
    profile_name: str
    profile_version: str
    duration_ms: float
    veto_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "band": self.band.value,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "breakdown": self.breakdown,
            "weights": self.weights,
            "duration_ms": round(self.duration_ms, 2),
            "veto_reason": self.veto_reason,
        }


# ---------------------------------------------------------------------------
# Scorer registry
# ---------------------------------------------------------------------------
class CriterionScorer(ABC):
    """One scoring criterion."""

    name: str = "criterion"

    @abstractmethod
    def score(
        self,
        tender: NormalizedTender,
        config: dict[str, Any],
        context: dict[str, Any],
    ) -> CriterionResult:
        """Return a value in [0, 1], or ``None`` when the criterion does not apply."""


_SCORERS: dict[str, CriterionScorer] = {}


def register_scorer(scorer: CriterionScorer) -> CriterionScorer:
    _SCORERS[scorer.name] = scorer
    return scorer


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


#: Extracted document text is capped before scoring. Beyond this, keyword and
#: field-of-work matching have long since saturated, while the cost of
#: normalising the string keeps growing.
_MAX_FULL_TEXT_CHARS = 60_000


def _tender_blob(tender: NormalizedTender) -> str:
    """The text every content criterion reads.

    Includes the text extracted from the tender's documents — without it, a PDF
    tender is judged on its title alone, and the two criteria that carry most
    of the weight have almost nothing to work with.
    """
    return normalize_text(
        " ".join(
            part
            for part in (
                tender.title,
                tender.description,
                tender.sector,
                tender.category,
                tender.buyer,
                (tender.full_text or "")[:_MAX_FULL_TEXT_CHARS],
            )
            if part
        )
    )


# ---------------------------------------------------------------------------
# Criteria
# ---------------------------------------------------------------------------
class FieldOfWorkScorer(CriterionScorer):
    """Semantic proximity to the company's declared fields of activity.

    The highest-weighted criterion, and the one that decides whether an
    opportunity is ours at all. Each profile is scored by the best match among
    its terms; the tender takes the best profile.
    """

    name = "field_of_work"

    def score(
        self, tender: NormalizedTender, config: dict[str, Any], context: dict[str, Any]
    ) -> CriterionResult:
        profiles = config.get("profiles") or []
        if not profiles:
            return CriterionResult(None, "No fields of work are configured.")

        blob = _tender_blob(tender)
        if not blob:
            return CriterionResult(None, "Tender has no text to compare.")

        backend = get_similarity_backend()
        floor = float(config.get("floor") or 0.0)

        best_profile: str | None = None
        best_score = 0.0
        matched_terms: list[str] = []

        for profile in profiles:
            terms = [str(t) for t in (profile.get("terms") or [])]
            if not terms:
                continue
            weight = float(profile.get("weight") or 1.0)

            # An exact term occurrence is far stronger evidence than semantic
            # proximity, so it short-circuits to a near-perfect profile score.
            hits = [term for term in terms if normalize_text(term) in blob]
            if hits:
                profile_score = 1.0
            else:
                profile_score = max(backend.similarity(blob, term) for term in terms)

            profile_score *= weight
            if profile_score > best_score:
                best_score = profile_score
                best_profile = str(profile.get("name") or "unnamed")
                matched_terms = hits[:5]

        if best_score < floor:
            return CriterionResult(
                0.0,
                "No configured field of work matches this tender.",
                {"best_score": round(best_score, 4), "floor": floor},
            )

        explanation = (
            f"Matches '{best_profile}'"
            + (f" on: {', '.join(matched_terms)}." if matched_terms else " semantically.")
        )
        return CriterionResult(
            _clamp(best_score),
            explanation,
            {"profile": best_profile, "matched_terms": matched_terms},
        )


class DeadlineProximityScorer(CriterionScorer):
    """Time left to respond.

    Not monotonic, and that is the point. A deadline in two days is worth
    almost nothing because we cannot realistically assemble a bid; a deadline
    in eight months is worth little because it is speculative. The value peaks
    in the window where a quality response is actually achievable.
    """

    name = "deadline_proximity"

    def score(
        self, tender: NormalizedTender, config: dict[str, Any], context: dict[str, Any]
    ) -> CriterionResult:
        if tender.deadline is None:
            return CriterionResult(
                float(config.get("unknown_deadline_score", 0.35)),
                "Deadline is not published; scored with the neutral default.",
            )

        now = context.get("now") or utc_now()
        days = (as_utc(tender.deadline) - as_utc(now)).total_seconds() / 86400.0

        if days < 0:
            return CriterionResult(
                float(config.get("expired_score", 0.0)),
                f"Deadline passed {abs(days):.0f} day(s) ago.",
                {"days_remaining": round(days, 2)},
            )

        min_viable = float(config.get("min_viable_days", 5))
        ideal_min = float(config.get("ideal_min_days", 12))
        ideal_max = float(config.get("ideal_max_days", 45))
        horizon = float(config.get("horizon_days", 180))

        if days < min_viable:
            # Steep decay: technically open, practically unreachable.
            value = 0.15 * (days / min_viable) if min_viable > 0 else 0.0
            explanation = f"Only {days:.1f} days left — too short to respond well."
        elif days < ideal_min:
            span = ideal_min - min_viable
            value = 0.15 + 0.85 * ((days - min_viable) / span if span > 0 else 1.0)
            explanation = f"{days:.0f} days left — tight but feasible."
        elif days <= ideal_max:
            value = 1.0
            explanation = f"{days:.0f} days left — ideal response window."
        elif days <= horizon:
            span = horizon - ideal_max
            value = 1.0 - 0.7 * ((days - ideal_max) / span if span > 0 else 1.0)
            explanation = f"{days:.0f} days left — distant deadline."
        else:
            value = 0.2
            explanation = f"{days:.0f} days left — beyond the planning horizon."

        return CriterionResult(_clamp(value), explanation, {"days_remaining": round(days, 2)})


class KeywordScorer(CriterionScorer):
    """Weighted keyword hits, with blocking terms acting as a veto."""

    name = "keywords"

    def score(
        self, tender: NormalizedTender, config: dict[str, Any], context: dict[str, Any]
    ) -> CriterionResult:
        blob = _tender_blob(tender)
        if not blob:
            return CriterionResult(None, "Tender has no text to search.")

        for blocked in config.get("exclude") or []:
            if normalize_text(str(blocked)) in blob:
                return CriterionResult(
                    0.0,
                    f"Contains the blocking term '{blocked}' — out of scope.",
                    {"blocked_term": str(blocked)},
                    veto=True,
                )

        include = config.get("include") or {}
        if not include:
            return CriterionResult(None, "No keywords are configured.")

        saturation = float(config.get("saturation_at") or 6)
        mode = str(config.get("match_mode") or "weighted")

        hits: dict[str, float] = {}
        for keyword, weight in include.items():
            if normalize_text(str(keyword)) in blob:
                hits[str(keyword)] = float(weight)

        if not hits:
            return CriterionResult(0.0, "No configured keyword appears in this tender.")

        total = sum(hits.values()) if mode == "weighted" else float(len(hits))
        value = _clamp(total / saturation) if saturation > 0 else 1.0
        top = sorted(hits, key=lambda k: hits[k], reverse=True)[:5]
        return CriterionResult(
            value,
            f"Matched {len(hits)} keyword(s): {', '.join(top)}.",
            {"matched": hits},
        )


class BudgetScorer(CriterionScorer):
    """Piecewise preference curve over the estimated amount."""

    name = "budget"

    def score(
        self, tender: NormalizedTender, config: dict[str, Any], context: dict[str, Any]
    ) -> CriterionResult:
        if tender.estimated_budget is None:
            return CriterionResult(
                float(config.get("unknown_budget_score", 0.5)),
                "Budget is not published; scored with the neutral default.",
            )

        reference_currency = str(config.get("currency") or "TND")
        rates: dict[str, float] = config.get("fx_to_currency") or {}
        currency = (tender.currency or reference_currency).upper()
        rate = float(rates.get(currency, 1.0))
        amount = float(Decimal(tender.estimated_budget)) * rate

        too_small = float(config.get("too_small_below", 0))
        ideal_min = float(config.get("ideal_min", too_small))
        ideal_max = float(config.get("ideal_max", ideal_min))
        too_large = float(config.get("too_large_above", ideal_max))

        details = {
            "amount": round(amount, 2),
            "currency": reference_currency,
            "original_currency": currency,
        }

        if amount < too_small:
            value = 0.1 * (amount / too_small) if too_small > 0 else 0.0
            explanation = f"{amount:,.0f} {reference_currency} — below the viable threshold."
        elif amount < ideal_min:
            span = ideal_min - too_small
            value = 0.1 + 0.9 * ((amount - too_small) / span if span > 0 else 1.0)
            explanation = f"{amount:,.0f} {reference_currency} — modest but workable."
        elif amount <= ideal_max:
            value = 1.0
            explanation = f"{amount:,.0f} {reference_currency} — in the target range."
        elif amount <= too_large:
            span = too_large - ideal_max
            value = 1.0 - 0.5 * ((amount - ideal_max) / span if span > 0 else 1.0)
            explanation = f"{amount:,.0f} {reference_currency} — large; may need partners."
        else:
            value = 0.35
            explanation = (
                f"{amount:,.0f} {reference_currency} — very large; realistically a consortium bid."
            )

        return CriterionResult(_clamp(value), explanation, details)


class _LookupScorer(CriterionScorer):
    """Shared implementation for the preference-table criteria."""

    field_name = ""
    label = ""

    def _value_of(self, tender: NormalizedTender) -> str | None:
        raise NotImplementedError

    def score(
        self, tender: NormalizedTender, config: dict[str, Any], context: dict[str, Any]
    ) -> CriterionResult:
        raw = self._value_of(tender)
        if not raw:
            return CriterionResult(
                float(config.get("unknown_score", 0.4)),
                f"{self.label} is unknown; scored with the neutral default.",
            )

        preferred: dict[str, float] = config.get("preferred") or {}
        needle = normalize_text(raw)
        best_key, best_value = None, None
        for candidate, value in preferred.items():
            normalized = normalize_text(str(candidate))
            if normalized and (normalized in needle or needle in normalized):
                if best_value is None or float(value) > best_value:
                    best_key, best_value = str(candidate), float(value)

        if best_value is not None:
            return CriterionResult(
                _clamp(best_value),
                f"{self.label} '{raw}' matches the preferred entry '{best_key}'.",
                {"matched": best_key},
            )
        return CriterionResult(
            float(config.get("default_score", 0.3)),
            f"{self.label} '{raw}' is not in the preferred list.",
        )


class LocationScorer(_LookupScorer):
    name = "location"
    label = "Location"

    def _value_of(self, tender: NormalizedTender) -> str | None:
        return " ".join(p for p in (tender.country, tender.location) if p) or None


class OrganizationScorer(_LookupScorer):
    name = "organization"
    label = "Buyer"

    def _value_of(self, tender: NormalizedTender) -> str | None:
        return " ".join(p for p in (tender.buyer, tender.funding_organization) if p) or None


class CpvSimilarityScorer(CriterionScorer):
    """Hierarchical CPV matching.

    CPV codes are a tree encoded in a number: the first two digits are the
    division, the next the group, then the class. A shared prefix therefore
    means "related procurement", and a longer shared prefix means "more
    related" — which is exactly the graded signal a score wants.
    """

    name = "cpv_similarity"

    def score(
        self, tender: NormalizedTender, config: dict[str, Any], context: dict[str, Any]
    ) -> CriterionResult:
        preferred = [str(c) for c in (config.get("preferred_codes") or [])]
        if not preferred:
            return CriterionResult(None, "No preferred CPV codes are configured.")
        if not tender.cpv_codes:
            return CriterionResult(
                float(config.get("unknown_score", 0.4)),
                "Tender publishes no CPV codes; scored with the neutral default.",
            )

        best_score, best_pair = 0.0, None
        for code in tender.cpv_codes:
            for target in preferred:
                shared = 0
                # Deliberately not strict: CPV codes of different lengths are
                # normal, and the shared prefix is exactly what we want.
                for a, b in zip(code, target, strict=False):
                    if a != b:
                        break
                    shared += 1
                if shared >= 2:
                    # 2 shared digits (division) -> 0.4; 8 (exact) -> 1.0.
                    value = 0.4 + 0.6 * ((shared - 2) / 6)
                    if value > best_score:
                        best_score, best_pair = value, (code, target)

        if best_pair is None:
            return CriterionResult(0.0, "No CPV code overlaps the preferred families.")
        return CriterionResult(
            _clamp(best_score),
            f"CPV {best_pair[0]} overlaps the preferred family {best_pair[1]}.",
            {"tender_code": best_pair[0], "preferred_code": best_pair[1]},
        )


class HistoricalSuccessScorer(CriterionScorer):
    """Past win rate with the same buyer or in the same sector.

    Holds the neutral prior until the sample is large enough to mean anything —
    a single lost bid must not condemn a buyer forever.
    """

    name = "historical_success"

    def score(
        self, tender: NormalizedTender, config: dict[str, Any], context: dict[str, Any]
    ) -> CriterionResult:
        provider: Callable[[NormalizedTender], tuple[int, int] | None] | None = context.get(
            "history_provider"
        )
        if provider is None:
            return CriterionResult(None, "No submission history is available.")
        try:
            stats = provider(tender)
        except Exception as exc:
            logger.warning("scoring.history_provider_failed", error=str(exc))
            return CriterionResult(None, "Submission history could not be read.")
        if not stats:
            return CriterionResult(None, "No submission history for this buyer.")

        wins, total = stats
        min_samples = int(config.get("min_samples", 5))
        prior = float(config.get("prior", 0.5))
        if total < min_samples:
            return CriterionResult(
                prior,
                f"Only {total} past submission(s) — using the neutral prior.",
                {"wins": wins, "total": total},
            )
        rate = wins / total if total else prior
        return CriterionResult(
            _clamp(rate),
            f"Won {wins} of {total} past submissions with this buyer.",
            {"wins": wins, "total": total},
        )


class ProcurementTypeScorer(CriterionScorer):
    name = "procurement_type"

    def score(
        self, tender: NormalizedTender, config: dict[str, Any], context: dict[str, Any]
    ) -> CriterionResult:
        if tender.procurement_type is ProcurementType.UNKNOWN:
            return CriterionResult(
                float(config.get("unknown_score", 0.5)),
                "Procedure type is unknown; scored with the neutral default.",
            )
        preferred: dict[str, float] = config.get("preferred") or {}
        value = preferred.get(tender.procurement_type.value)
        if value is None:
            return CriterionResult(
                float(config.get("unknown_score", 0.5)),
                f"Procedure '{tender.procurement_type.value}' has no configured preference.",
            )
        return CriterionResult(
            _clamp(float(value)),
            f"Procedure type '{tender.procurement_type.value}'.",
        )


for _scorer in (
    FieldOfWorkScorer(),
    DeadlineProximityScorer(),
    KeywordScorer(),
    BudgetScorer(),
    LocationScorer(),
    OrganizationScorer(),
    CpvSimilarityScorer(),
    HistoricalSuccessScorer(),
    ProcurementTypeScorer(),
):
    register_scorer(_scorer)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class ScoringEngine:
    """Combines registered criteria according to the configured profile."""

    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.profile = profile or load_yaml_config("scoring")
        self.name = str(self.profile.get("name") or "default")
        self.version = str(self.profile.get("version") or "0")
        self.weights: dict[str, float] = {
            key: float(value)
            for key, value in (self.profile.get("weights") or {}).items()
            if float(value) > 0
        }
        self.criteria_config: dict[str, Any] = self.profile.get("criteria") or {}
        self.bands = self._parse_bands(self.profile.get("bands") or {})

    @staticmethod
    def _parse_bands(raw: dict[str, Any]) -> list[tuple[float, RelevanceBand]]:
        mapping = {
            "highly_relevant": RelevanceBand.HIGHLY_RELEVANT,
            "relevant": RelevanceBand.RELEVANT,
            "low_relevance": RelevanceBand.LOW_RELEVANCE,
        }
        bands = [
            (float((raw.get(key) or {}).get("min_score", 0.0)), band)
            for key, band in mapping.items()
            if key in raw
        ]
        bands.sort(key=lambda item: item[0], reverse=True)
        return bands or [(0.75, RelevanceBand.HIGHLY_RELEVANT), (0.5, RelevanceBand.RELEVANT),
                         (0.0, RelevanceBand.LOW_RELEVANCE)]

    def band_for(self, score: float) -> RelevanceBand:
        for threshold, band in self.bands:
            if score >= threshold:
                return band
        return RelevanceBand.LOW_RELEVANCE

    def band_metadata(self) -> dict[str, dict[str, Any]]:
        """Labels and colours for the dashboard badges."""
        return {
            key: {
                "label": value.get("label"),
                "color": value.get("color"),
                "min_score": value.get("min_score"),
            }
            for key, value in (self.profile.get("bands") or {}).items()
        }

    # ------------------------------------------------------------------
    def score(
        self, tender: NormalizedTender, *, context: dict[str, Any] | None = None
    ) -> ScoreResult:
        started = time.perf_counter()
        context = dict(context or {})
        breakdown: dict[str, dict[str, Any]] = {}
        weighted_total = 0.0
        applied_weight = 0.0
        veto_reason: str | None = None

        for criterion, weight in self.weights.items():
            scorer = _SCORERS.get(criterion)
            if scorer is None:
                logger.warning("scoring.unknown_criterion", criterion=criterion)
                continue

            config = self.criteria_config.get(criterion) or {}
            try:
                result = scorer.score(tender, config, context)
            except Exception as exc:
                # Degrade to the remaining criteria: a bug in one scorer must
                # not leave the tender unscored and therefore invisible.
                logger.warning(
                    "scoring.criterion_failed",
                    criterion=criterion,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                breakdown[criterion] = {
                    "value": None,
                    "weight": weight,
                    "weighted": 0.0,
                    "explanation": "This criterion could not be evaluated.",
                    "error": type(exc).__name__,
                }
                continue

            if result.veto:
                veto_reason = result.explanation

            contribution = (result.value or 0.0) * weight if result.applicable else 0.0
            breakdown[criterion] = {
                "value": round(result.value, 4) if result.applicable else None,
                "weight": weight,
                "weighted": round(contribution, 4),
                "explanation": result.explanation,
                "details": result.details,
                "applicable": result.applicable,
            }
            if result.applicable:
                weighted_total += contribution
                applied_weight += weight

        if veto_reason:
            duration_ms = (time.perf_counter() - started) * 1000
            scoring_runs_total.labels(profile_version=self.version, outcome="vetoed").inc()
            return ScoreResult(
                score=0.0,
                band=RelevanceBand.OUT_OF_SCOPE,
                breakdown=breakdown,
                weights=dict(self.weights),
                profile_name=self.name,
                profile_version=self.version,
                duration_ms=duration_ms,
                veto_reason=veto_reason,
            )

        # Renormalise over the weight that was actually applicable, so missing
        # data is neutral rather than punitive.
        final = weighted_total / applied_weight if applied_weight > 0 else 0.0
        final = _clamp(final)
        band = self.band_for(final)
        duration_ms = (time.perf_counter() - started) * 1000

        scoring_runs_total.labels(profile_version=self.version, outcome="scored").inc()
        tender_score.labels(band=band.value).observe(final)

        return ScoreResult(
            score=final,
            band=band,
            breakdown=breakdown,
            weights=dict(self.weights),
            profile_name=self.name,
            profile_version=self.version,
            duration_ms=duration_ms,
        )


_engine: ScoringEngine | None = None


def get_scoring_engine() -> ScoringEngine:
    global _engine
    if _engine is None:
        _engine = ScoringEngine()
    return _engine


def reset_scoring_engine() -> None:
    """Reload the profile — called after a weights edit through the admin API."""
    global _engine
    _engine = None
