"""Freezing a ranking, and what a human is allowed to do to it afterwards.

The tests worth having here are not about storage. They are about the three
places where the convenient behaviour and the honest one differ, and where a
future reader will be tempted to swap one for the other:

* a capture keeps the profiles the ranking refused, not just its winners;
* validating never converts a silence into a decision;
* a validated capture stops accepting edits.

Each is cheap to break by accident and expensive to notice, because the wrong
version still produces a plausible document.
"""

from __future__ import annotations

import uuid as uuid_module

import pytest

from app.core.enums import ShortlistDecision, ShortlistStatus
from app.db.models.shortlist import Shortlist, ShortlistEntry
from app.services.shortlist import (
    ShortlistLocked,
    apply_decision,
    build_shortlist,
    retained_entries,
    summarise,
    validate_shortlist,
)


def _candidate(**overrides):
    base = {
        "cv_id": str(uuid_module.uuid4()),
        "filename": "cv_dupont.pdf",
        "display_name": "Marie Dupont",
        "label": "Marie Dupont",
        "score": 0.81,
        "similarity": 0.74,
        "coverage": 0.66,
        "technology_ratio": 0.9,
        "matched_technologies": ["Java", "Spring"],
        "missing_technologies": ["Kubernetes"],
        "vetoed": False,
        "veto_reason": None,
        "evidence": [{"requirement": 1, "score": 0.7, "passage": "Développement Java 17"}],
    }
    base.update(overrides)
    return base


def _outcome(**overrides):
    base = {
        "tender_id": str(uuid_module.uuid4()),
        "title": "Développement d'une application web",
        "status": "ok",
        "requirements": [{"position": 1, "document": "CCTP", "text": "Java, Spring Boot"}],
        "required_technologies": ["Java", "Spring", "Kubernetes"],
        "structured_requirements": {"technologies": ["Java"]},
        "kept_total": 41,
        "vetoed_total": 244,
        "weights": {"version": "match-v2", "semantic": 0.6},
        "candidates": [
            _candidate(),
            _candidate(label="Ahmed Ben Ali", display_name="Ahmed Ben Ali", score=0.62),
            _candidate(
                label="Sonia Kacem",
                display_name="Sonia Kacem",
                score=0.0,
                vetoed=True,
                veto_reason="1 of 3 required technologies evidenced",
            ),
        ],
    }
    base.update(overrides)
    return base


class TestACaptureKeepsTheQuestion:
    """A score without its scale is a number nobody can argue with, and
    arguing with it is exactly what a bid manager should do."""

    def test_the_requirements_and_weights_are_frozen_alongside(self):
        shortlist = build_shortlist(_outcome(), tenant="inetum", created_by="ahmed")

        assert shortlist.requirements[0]["text"] == "Java, Spring Boot"
        assert shortlist.required_technologies == ["Java", "Spring", "Kubernetes"]
        assert shortlist.weights["version"] == "match-v2"

    def test_the_totals_cover_everything_considered(self):
        """The interface once displayed "0 écartés" on a run where 244 profiles
        had been vetoed: refused profiles score zero and never reach a top-20
        cut. The count and the stored sample are two different answers."""
        shortlist = build_shortlist(_outcome(), tenant="inetum")

        assert shortlist.vetoed_total == 244
        assert len(shortlist.entries) == 3

    def test_refused_profiles_are_stored_not_dropped(self):
        """A shortlist showing only its winners cannot be challenged."""
        shortlist = build_shortlist(_outcome(), tenant="inetum")
        refused = [e for e in shortlist.entries if e.vetoed]

        assert len(refused) == 1
        assert refused[0].veto_reason == "1 of 3 required technologies evidenced"

    def test_the_label_is_copied_rather_than_joined(self):
        """A CV re-imported under a new display name must not rewrite what the
        validator had on screen."""
        shortlist = build_shortlist(_outcome(), tenant="inetum")

        assert shortlist.entries[0].label == "Marie Dupont"

    def test_ranks_are_explicit_and_start_at_one(self):
        shortlist = build_shortlist(_outcome(), tenant="inetum")

        assert [e.rank for e in shortlist.entries] == [1, 2, 3]

    def test_an_unfamiliar_field_does_not_break_the_capture(self):
        """The matching task's shape is allowed to grow. A capture that raised
        on an unknown key would take the feature down for an addition that
        concerns it not at all."""
        outcome = _outcome()
        outcome["some_future_signal"] = {"whatever": 1}
        outcome["candidates"][0]["future_metric"] = 0.5

        shortlist = build_shortlist(outcome, tenant="inetum")

        assert len(shortlist.entries) == 3

    def test_a_missing_score_becomes_zero_rather_than_none(self):
        outcome = _outcome(candidates=[{"cv_id": str(uuid_module.uuid4())}])
        shortlist = build_shortlist(outcome, tenant="inetum")

        assert shortlist.entries[0].score == 0.0
        assert shortlist.entries[0].label == ""


class TestValidationNeverInventsADecision:
    def test_pending_candidates_stay_pending(self):
        """Marking every untouched row as rejected would be one click instead
        of twenty — and would write "rejected by Ahmed" against profiles Ahmed
        never opened. "Not selected" and "turned down" are different findings,
        and Phase 8 will want to tell them apart."""
        shortlist = build_shortlist(_outcome(), tenant="inetum")
        apply_decision(shortlist.entries[0], decision=ShortlistDecision.RETAINED, actor="ahmed")

        validate_shortlist(shortlist, actor="ahmed")

        assert shortlist.entries[1].decision == ShortlistDecision.PENDING.value
        assert shortlist.entries[1].decided_by is None

    def test_only_explicit_retentions_reach_generation(self):
        shortlist = build_shortlist(_outcome(), tenant="inetum")
        apply_decision(shortlist.entries[0], decision=ShortlistDecision.RETAINED, actor="ahmed")
        apply_decision(shortlist.entries[1], decision=ShortlistDecision.REJECTED, actor="ahmed")

        kept = retained_entries(shortlist)

        assert [e.label for e in kept] == ["Marie Dupont"]

    def test_an_empty_selection_can_still_be_validated(self):
        """"We looked and nobody fits" is a real conclusion, and as informative
        as a loss when tuning relevance later."""
        shortlist = build_shortlist(_outcome(), tenant="inetum")

        validate_shortlist(shortlist, actor="ahmed")

        assert shortlist.status == ShortlistStatus.VALIDATED.value
        assert retained_entries(shortlist) == []

    def test_validation_records_who_and_when(self):
        shortlist = build_shortlist(_outcome(), tenant="inetum")
        validate_shortlist(shortlist, actor="hanene")

        assert shortlist.validated_by == "hanene"
        assert shortlist.validated_at is not None


class TestAValidatedCaptureIsLocked:
    """The human lock has to bite, or the generated documents stop
    corresponding to anything anyone signed."""

    def test_a_decision_after_validation_is_refused(self):
        shortlist = build_shortlist(_outcome(), tenant="inetum")
        # The relationship is what `apply_decision` consults, and it is only
        # populated once both sides are linked — which `build_shortlist` does.
        validate_shortlist(shortlist, actor="ahmed")

        with pytest.raises(ShortlistLocked):
            apply_decision(
                shortlist.entries[0], decision=ShortlistDecision.RETAINED, actor="ahmed"
            )

    def test_validating_twice_is_refused(self):
        shortlist = build_shortlist(_outcome(), tenant="inetum")
        validate_shortlist(shortlist, actor="ahmed")

        with pytest.raises(ShortlistLocked):
            validate_shortlist(shortlist, actor="hanene")

    def test_an_entry_with_no_shortlist_is_still_editable(self):
        """Guards must not depend on an object graph being fully wired: a
        detached entry is not a locked one."""
        entry = ShortlistEntry(rank=1, label="Orphan")

        apply_decision(entry, decision=ShortlistDecision.RETAINED, actor="ahmed")

        assert entry.decision == ShortlistDecision.RETAINED.value


class TestOverridingTheVetoIsAllowedButVisible:
    def test_a_vetoed_profile_can_be_retained(self):
        """The veto is a lexical rule over a technology list read out of a
        document. A bid manager who knows the consultant is the authority."""
        shortlist = build_shortlist(_outcome(), tenant="inetum")
        vetoed = shortlist.entries[2]

        apply_decision(
            vetoed,
            decision=ShortlistDecision.RETAINED,
            actor="ahmed",
            note="A fait le projet SAP chez le même client en 2024.",
        )

        assert vetoed.decision == ShortlistDecision.RETAINED.value
        # Still flagged: the record must show a human went against the ranking
        # rather than hide that they did.
        assert vetoed.vetoed is True
        assert vetoed.veto_reason


class TestTheApiShape:
    def test_a_listing_omits_the_evidence(self):
        """A capture holds up to twenty-eight candidates with their passages.
        A page of twenty-five of those is megabytes for a screen of titles."""
        shortlist = build_shortlist(_outcome(), tenant="inetum")

        payload = summarise(shortlist, include_entries=False)

        assert "entries" not in payload
        assert payload["vetoed_total"] == 244

    def test_the_detail_counts_each_decision(self):
        shortlist = build_shortlist(_outcome(), tenant="inetum")
        apply_decision(shortlist.entries[0], decision=ShortlistDecision.RETAINED, actor="a")

        payload = summarise(shortlist)

        assert payload["decisions"]["retained"] == 1
        assert payload["decisions"]["pending"] == 2
        assert len(payload["entries"]) == 3


class TestItSurvivesTheRoundTrip:
    def test_a_capture_is_stored_and_read_back_whole(self, db_session):
        shortlist = build_shortlist(_outcome(), tenant="inetum", created_by="ahmed")
        db_session.add(shortlist)
        db_session.commit()

        fetched = db_session.get(Shortlist, shortlist.id)

        assert fetched is not None
        assert len(fetched.entries) == 3
        assert fetched.entries[0].evidence[0]["passage"] == "Développement Java 17"
        assert fetched.required_technologies == ["Java", "Spring", "Kubernetes"]

    def test_deleting_a_capture_takes_its_entries(self, db_session):
        shortlist = build_shortlist(_outcome(), tenant="inetum")
        db_session.add(shortlist)
        db_session.commit()

        db_session.delete(shortlist)
        db_session.commit()

        assert db_session.query(ShortlistEntry).count() == 0
