"""Duplicate detection across all three stages."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.enums import DuplicateStrategy, EntryPoint
from app.core.identity import content_fingerprint, utc_now
from app.db.models.tender import DuplicateRecord, Tender
from app.services.deduplication import DeduplicationService
from app.services.similarity import LexicalBackend


@pytest.fixture()
def dedup() -> DeduplicationService:
    return DeduplicationService()


def _persist(session, tender, **overrides):
    """Insert a tender the way the ingestion service would.

    ``created_at`` is set explicitly because the column's server default is
    only materialised by the database on INSERT, and the semantic stage's
    candidate window filters on it — a committed production row always has it.
    """
    fingerprint = content_fingerprint(text=tender.comparison_text())
    overrides.setdefault("created_at", utc_now())
    row = Tender(
        source_key=tender.connector_key,
        entry_point=EntryPoint.MANUAL_SCRAPE.value,
        source_url=tender.source_url,
        canonical_url=tender.canonical_url,
        external_id=tender.external_id,
        reference=tender.reference,
        title=tender.title,
        description=tender.description,
        buyer=tender.buyer,
        country=tender.country,
        deadline=tender.deadline,
        text_sha256=fingerprint["text_sha256"],
        **overrides,
    )
    session.add(row)
    session.flush()
    return row


class TestCanonicalUrlStage:
    def test_same_url_is_a_duplicate(self, db_session, dedup, make_tender):
        original = make_tender()
        _persist(db_session, original)

        verdict = dedup.check(db_session, make_tender())
        assert verdict.is_duplicate
        assert verdict.strategy is DuplicateStrategy.CANONICAL_URL

    def test_tracking_parameters_do_not_defeat_it(self, db_session, dedup, make_tender):
        _persist(db_session, make_tender())

        noisy = make_tender(
            source_url="https://portal.example.tn/avis/1?utm_source=newsletter&sessionid=zz",
            canonical_url=None,
        )
        noisy.canonical_url = dedup.canonicalize(noisy.source_url)
        assert dedup.check(db_session, noisy).is_duplicate

    def test_a_genuinely_different_url_is_not_a_duplicate(
        self, db_session, dedup, make_tender
    ):
        _persist(db_session, make_tender())
        different = make_tender(
            source_url="https://portal.example.tn/avis/2",
            canonical_url="https://portal.example.tn/avis/2",
            title="Fourniture de matériel de bureau pour les services centraux",
            description="Fourniture et livraison de mobilier et de consommables.",
            reference="AO 99/2026",
        )
        assert dedup.check(db_session, different).is_duplicate is False


class TestExternalIdStage:
    def test_same_portal_identifier_is_a_duplicate(self, db_session, dedup, make_tender):
        _persist(db_session, make_tender(external_id="TN-2026-001"))
        candidate = make_tender(
            external_id="TN-2026-001",
            source_url="https://portal.example.tn/avis/other",
            canonical_url="https://portal.example.tn/avis/other",
        )
        verdict = dedup.check(db_session, candidate)
        assert verdict.strategy is DuplicateStrategy.EXTERNAL_ID

    def test_same_identifier_on_a_different_portal_is_not(
        self, db_session, dedup, make_tender
    ):
        _persist(db_session, make_tender(external_id="001"))
        other_portal = make_tender(
            connector_key="j360",
            external_id="001",
            source_url="https://j360.info/t/001",
            canonical_url="https://j360.info/t/001",
            title="Un marché totalement différent de fournitures diverses",
            description="Achat de consommables de bureau.",
            reference="J-001",
        )
        assert other_portal.connector_key == "j360"
        verdict = dedup.check(db_session, other_portal)
        assert verdict.strategy is not DuplicateStrategy.EXTERNAL_ID


class TestContentHashStage:
    def test_identical_bytes_from_another_portal_collide(
        self, db_session, dedup, make_tender
    ):
        raw = b"%PDF-1.4 identical bytes"
        digest = content_fingerprint(raw=raw)["raw_sha256"]
        _persist(db_session, make_tender(), raw_sha256=digest)

        mirrored = make_tender(
            connector_key="j360",
            source_url="https://j360.info/t/9",
            canonical_url="https://j360.info/t/9",
            external_id=None,
        )
        verdict = dedup.check(db_session, mirrored, raw_sha256=digest)
        assert verdict.is_duplicate
        assert verdict.strategy is DuplicateStrategy.RAW_HASH

    def test_normalised_text_hash_survives_reformatting(
        self, db_session, dedup, make_tender
    ):
        original = make_tender()
        _persist(db_session, original)

        reformatted = make_tender(
            source_url="https://portal.example.tn/avis/1-bis",
            canonical_url="https://portal.example.tn/avis/1-bis",
            external_id=None,
            title=original.title.upper() + "  ",
        )
        digest = content_fingerprint(text=reformatted.comparison_text())["text_sha256"]
        verdict = dedup.check(db_session, reformatted, text_sha256=digest)
        assert verdict.is_duplicate


class TestSemanticStage:
    def test_republished_notice_is_caught(self, db_session, dedup, make_tender):
        _persist(db_session, make_tender())

        republished = make_tender(
            source_url="https://portal.example.tn/avis/1-rectificatif",
            canonical_url="https://portal.example.tn/avis/1-rectificatif",
            external_id=None,
            reference="AO 01/2026 rectificatif",
        )
        verdict = dedup.check(db_session, republished)
        assert verdict.is_duplicate
        assert verdict.strategy is DuplicateStrategy.SEMANTIC
        assert verdict.similarity >= dedup.threshold

    def test_a_different_tender_from_the_same_buyer_is_kept(
        self, db_session, dedup, make_tender
    ):
        _persist(db_session, make_tender())

        different = make_tender(
            source_url="https://portal.example.tn/avis/77",
            canonical_url="https://portal.example.tn/avis/77",
            external_id=None,
            reference="AO 77/2026",
            title="Fourniture de véhicules utilitaires pour le parc automobile",
            description="Acquisition de dix véhicules utilitaires légers et leur entretien.",
        )
        verdict = dedup.check(db_session, different)
        assert verdict.is_duplicate is False

    def test_candidate_window_bounds_the_comparison(self, db_session, dedup, make_tender):
        """Stage 3 must stay O(k), not O(n) over the whole corpus."""
        old = _persist(db_session, make_tender())
        old.created_at = utc_now() - timedelta(days=dedup.lookback_days + 30)
        db_session.flush()

        candidate = make_tender(
            source_url="https://portal.example.tn/avis/1-again",
            canonical_url="https://portal.example.tn/avis/1-again",
            external_id=None,
        )
        # Outside the lookback window, so it is not even compared.
        assert len(dedup._candidates(db_session, candidate)) == 0


class TestEvidenceRecording:
    def test_rejection_is_recorded_and_provenance_updated(
        self, db_session, dedup, make_tender
    ):
        canonical = _persist(db_session, make_tender())
        canonical.seen_on_sources = ["fixture"]
        db_session.flush()

        incoming = make_tender(connector_key="j360")
        verdict = dedup.check(db_session, incoming)
        dedup.record_duplicate(db_session, incoming, verdict)
        db_session.flush()

        record = db_session.query(DuplicateRecord).one()
        assert record.canonical_tender_id == canonical.id
        assert record.source_key == "j360"
        assert record.payload["title"] == incoming.title

        db_session.refresh(canonical)
        assert canonical.duplicate_hits == 1
        # "Seen on 3 portals" is a stronger signal than "seen once".
        assert "j360" in canonical.seen_on_sources


class TestLexicalBackendDeterminism:
    def test_identical_text_scores_exactly_one(self):
        backend = LexicalBackend()
        assert backend.similarity("marché public", "marché public") == 1.0

    def test_unrelated_text_scores_low(self):
        backend = LexicalBackend()
        score = backend.similarity(
            "développement d'une application web de gestion documentaire",
            "fourniture de carburant pour le parc automobile municipal",
        )
        assert score < 0.3

    def test_small_edits_stay_similar(self):
        """Accents, hyphenation and casing must not make a notice look new.

        This is exactly why the backend blends character n-grams with word
        tokens: on tokens alone, "plateforme" and "plate-forme" share nothing.
        """
        backend = LexicalBackend()
        score = backend.similarity(
            "Développement d'une plateforme de gestion documentaire",
            "Developpement d'une plate-forme de gestion documentaire",
        )
        assert score > 0.7

    def test_results_are_reproducible(self):
        first = LexicalBackend().similarity("a b c developpement", "a b c maintenance")
        second = LexicalBackend().similarity("a b c developpement", "a b c maintenance")
        assert first == second

    def test_empty_input_scores_zero(self):
        assert LexicalBackend().similarity("", "anything") == 0.0
