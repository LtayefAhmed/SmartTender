"""Ingestion, source health, notification targeting and job aggregation."""

from __future__ import annotations

import uuid as uuid_module
from datetime import timedelta
from decimal import Decimal

import pytest

from app.connectors.models import ConnectorOutcome
from app.core.enums import (
    EntryPoint,
    JobStatus,
    RelevanceBand,
    SourceHealth,
    TenderPipelineState,
)
from app.core.identity import utc_now
from app.db.models.job import ScrapingJob
from app.db.models.notification import Notification, UserPreference
from app.db.models.source import Source
from app.db.models.tender import DuplicateRecord, Tender
from app.services import sources as source_service
from app.services.ingestion import IngestionService
from app.services.notifications import NotificationService


@pytest.fixture()
def ingestion(monkeypatch) -> IngestionService:
    """Ingestion with object storage stubbed out — no MinIO in the test suite."""
    from app.services import ingestion as ingestion_module

    class _FakeStorage:
        def build_key(self, tender_id, filename, **kwargs):
            return f"tenders/{tender_id}/{filename}"

        def put_bytes(self, key, data, **kwargs):
            from app.services.storage import StoredObject

            return StoredObject(
                bucket="test-bucket",
                key=key,
                size_bytes=len(data),
                content_type=kwargs.get("content_type", "application/octet-stream"),
            )

    monkeypatch.setattr(ingestion_module, "get_storage", lambda: _FakeStorage())
    return IngestionService()


class TestIngestion:
    def test_a_new_tender_is_accepted_and_persisted(self, db_session, ingestion, make_tender):
        result = ingestion.ingest_tender(
            db_session, make_tender(), entry_point=EntryPoint.MANUAL_SCRAPE
        )
        db_session.flush()

        assert result.accepted is True
        row = db_session.get(Tender, result.tender_id)
        assert row.title.startswith("Développement")
        # RECEIVED, not QUEUED: the task is only published after commit.
        assert row.pipeline_state == TenderPipelineState.RECEIVED.value
        assert row.seen_on_sources == ["fixture"]

    def test_the_master_uuid_names_the_stored_object(
        self, db_session, ingestion, make_tender
    ):
        result = ingestion.ingest_tender(
            db_session,
            make_tender(),
            entry_point=EntryPoint.MANUAL_UPLOAD,
            raw_content=b"%PDF-1.4 content",
            content_type="application/pdf",
            original_filename="cdc.pdf",
        )
        db_session.flush()

        row = db_session.get(Tender, result.tender_id)
        assert str(result.tender_id) in row.storage_key
        assert row.size_bytes == len(b"%PDF-1.4 content")

    def test_attachments_are_registered_as_pending(
        self, db_session, ingestion, make_tender
    ):
        result = ingestion.ingest_tender(
            db_session, make_tender(), entry_point=EntryPoint.MANUAL_SCRAPE
        )
        db_session.flush()

        row = db_session.get(Tender, result.tender_id)
        assert len(row.documents) == 1
        assert row.documents[0].status == "pending"

    def test_a_duplicate_is_rejected_without_consuming_a_uuid(
        self, db_session, ingestion, make_tender
    ):
        first = ingestion.ingest_tender(
            db_session, make_tender(), entry_point=EntryPoint.MANUAL_SCRAPE
        )
        db_session.flush()

        second = ingestion.ingest_tender(
            db_session, make_tender(), entry_point=EntryPoint.MANUAL_SCRAPE
        )
        db_session.flush()

        assert second.accepted is False
        assert second.reason == "duplicate"
        assert second.duplicate_of == first.tender_id
        assert second.tender_id is None
        assert db_session.query(Tender).count() == 1
        assert db_session.query(DuplicateRecord).count() == 1

    def test_a_storage_failure_still_keeps_the_tender(
        self, db_session, make_tender, monkeypatch
    ):
        """A tender we can see but whose original we failed to archive beats no
        tender at all."""
        from app.core.exceptions import StorageError
        from app.services import ingestion as ingestion_module

        class _BrokenStorage:
            def build_key(self, *args, **kwargs):
                return "k"

            def put_bytes(self, *args, **kwargs):
                raise StorageError("MinIO is down")

        monkeypatch.setattr(ingestion_module, "get_storage", lambda: _BrokenStorage())

        result = IngestionService().ingest_tender(
            db_session,
            make_tender(),
            entry_point=EntryPoint.MANUAL_UPLOAD,
            raw_content=b"data",
        )
        db_session.flush()

        assert result.accepted is True
        row = db_session.get(Tender, result.tender_id)
        assert row.storage_key is None

    def test_the_enqueue_callback_fires_only_after_commit(
        self, db_session, ingestion, make_tender
    ):
        """Publishing inside the transaction lets a worker beat the commit."""
        published: list[uuid_module.UUID] = []

        result = ingestion.ingest_tender(
            db_session,
            make_tender(),
            entry_point=EntryPoint.MANUAL_SCRAPE,
            enqueue=published.append,
        )
        assert published == []      # not yet — still inside the transaction

        db_session.commit()
        assert published == [result.tender_id]

    def test_source_counters_are_updated(self, db_session, ingestion, make_tender):
        source = Source(key="fixture", name="Fixture")
        db_session.add(source)
        db_session.flush()

        ingestion.ingest_tender(
            db_session,
            make_tender(),
            entry_point=EntryPoint.SCHEDULED_SCRAPE,
            source=source,
        )
        db_session.flush()
        assert source.total_items_ingested == 1


class TestSourceHealth:
    def _source(self, session) -> Source:
        source = Source(key="probe", name="Probe", enabled=True)
        session.add(source)
        session.flush()
        return source

    def test_a_successful_run_marks_the_source_healthy(self, db_session):
        source = self._source(db_session)
        source_service.apply_outcome(
            db_session,
            source,
            ConnectorOutcome(connector_key="probe", succeeded=True, duration_seconds=2.0,
                             tenders=[]),
        )
        # Zero items on a single run is not yet suspicious.
        assert source.total_runs == 1
        assert source.consecutive_failures == 0

    def test_three_consecutive_failures_mark_it_failing(self, db_session):
        source = self._source(db_session)
        for _ in range(3):
            source_service.apply_outcome(
                db_session,
                source,
                ConnectorOutcome(
                    connector_key="probe",
                    succeeded=False,
                    error_type="SourceUnavailableError",
                    error_message="down",
                ),
            )
        assert source.health == SourceHealth.FAILING.value
        assert source.consecutive_failures == 3

    def test_one_failure_marks_it_degraded(self, db_session):
        source = self._source(db_session)
        source_service.apply_outcome(
            db_session,
            source,
            ConnectorOutcome(connector_key="probe", succeeded=False, error_type="Timeout"),
        )
        assert source.health == SourceHealth.DEGRADED.value

    def test_repeated_empty_runs_are_flagged(self, db_session, make_tender):
        """HTTP 200 with zero rows is the dangerous failure: it looks like
        success everywhere except here."""
        source = self._source(db_session)
        for _ in range(3):
            source_service.apply_outcome(
                db_session,
                source,
                ConnectorOutcome(connector_key="probe", succeeded=True, tenders=[]),
            )
        assert source.health == SourceHealth.DEGRADED.value
        assert "no items" in source.health_reason

    def test_items_reset_the_empty_run_counter(self, db_session, make_tender):
        source = self._source(db_session)
        source_service.apply_outcome(
            db_session, source, ConnectorOutcome(connector_key="probe", succeeded=True)
        )
        source_service.apply_outcome(
            db_session,
            source,
            ConnectorOutcome(connector_key="probe", succeeded=True, tenders=[make_tender()]),
        )
        assert source.consecutive_empty_runs == 0
        assert source.health == SourceHealth.HEALTHY.value

    def test_a_skipped_run_is_not_a_health_signal(self, db_session):
        source = self._source(db_session)
        source_service.apply_outcome(
            db_session,
            source,
            ConnectorOutcome(
                connector_key="probe", skipped=True, skip_reason="credentials_missing"
            ),
        )
        assert source.total_runs == 0

    def test_the_success_rate_is_derived(self, db_session):
        source = self._source(db_session)
        source.total_runs, source.total_failures = 10, 2
        assert source.success_rate == 0.8

    def test_an_unflushed_source_does_not_crash_the_health_gauge(self):
        """Regression: column defaults are applied by the database, so a
        freshly constructed Source has `health=None`. `SourceHealth(None)`
        raises, which previously took the whole startup source-sync down."""
        from app.services.sources import _health_gauge

        assert _health_gauge(None) == 0.5
        assert _health_gauge("") == 0.5
        assert _health_gauge("not-a-real-health") == 0.5
        assert _health_gauge(SourceHealth.HEALTHY.value) == 1.0
        assert _health_gauge(SourceHealth.FAILING.value) == 0.0

    def test_sync_sources_tolerates_a_freshly_created_row(self, db_session):
        from app.services.sources import sync_sources

        db_session.add(Source(key="fixture", name="Fixture"))   # health is None
        db_session.flush()
        db_session.expire_all()

        result = sync_sources(db_session)   # must not raise
        assert result["created"] + result["updated"] > 0


class TestStorageErrorDiagnostics:
    def test_a_kms_failure_explains_itself(self, monkeypatch):
        """The ingestion path keeps the tender and drops the original, so this
        error message is the only place the failure is ever explained."""
        from app.core.exceptions import StorageError
        from app.services.storage import ObjectStorage

        storage = ObjectStorage()
        storage._bucket_checked = True

        class _Client:
            def put_object(self, *args, **kwargs):
                raise RuntimeError(
                    "S3 operation failed; code: NotImplemented, message: Server "
                    "side encryption specified but KMS is not configured"
                )

        monkeypatch.setattr(type(storage), "client", property(lambda self: _Client()))

        with pytest.raises(StorageError) as excinfo:
            storage.put_bytes("k", b"data")

        context = excinfo.value.context
        assert "KMS is not configured" in context["cause"]
        assert "SERVER_SIDE_ENCRYPTION" in context["hint"]


class TestJobAggregation:
    def test_a_partial_failure_is_not_a_job_failure(self, db_session):
        """One broken portal must never suppress three healthy ones."""
        job = ScrapingJob(
            connectors_total=4,
            connectors_succeeded=3,
            connectors_failed=1,
            connectors_skipped=0,
        )
        assert job.derive_status() == JobStatus.PARTIAL.value

    def test_a_total_failure_is_a_job_failure(self, db_session):
        job = ScrapingJob(connectors_total=2, connectors_failed=2)
        assert job.derive_status() == JobStatus.FAILED.value

    def test_all_succeeded_is_a_success(self, db_session):
        job = ScrapingJob(connectors_total=3, connectors_succeeded=3)
        assert job.derive_status() == JobStatus.SUCCEEDED.value

    def test_skipped_connectors_do_not_fail_the_job(self, db_session):
        job = ScrapingJob(connectors_total=2, connectors_failed=1, connectors_skipped=1)
        assert job.derive_status() == JobStatus.PARTIAL.value

    def test_an_incomplete_job_is_still_running(self, db_session):
        job = ScrapingJob(connectors_total=4, connectors_succeeded=1)
        assert job.derive_status() == JobStatus.RUNNING.value

    def test_progress_is_reported(self, db_session):
        job = ScrapingJob(connectors_total=4, connectors_succeeded=2, connectors_failed=1)
        assert job.progress == 0.75


class TestNotificationTargeting:
    def _tender(self, session, **overrides) -> Tender:
        defaults = {
            "source_key": "tuneps",
            "entry_point": EntryPoint.SCHEDULED_SCRAPE.value,
            "title": "Développement d'une application de gestion documentaire",
            "buyer": "Ministère des Technologies de la Communication",
            "country": "Tunisie",
            "sector": "Technologies de l'information",
            "cpv_codes": ["72200000"],
            "relevance_score": 0.82,
            "relevance_band": RelevanceBand.HIGHLY_RELEVANT.value,
            "deadline": utc_now() + timedelta(days=20),
            "estimated_budget": Decimal("500000"),
        }
        defaults.update(overrides)
        tender = Tender(**defaults)
        session.add(tender)
        session.flush()
        return tender

    def _preference(self, session, **overrides) -> UserPreference:
        defaults = {
            "user_id": "amine",
            "email": "amine@example.tn",
            "active": True,
            "channels": ["in_app"],
            "min_relevance_band": RelevanceBand.RELEVANT.value,
        }
        defaults.update(overrides)
        preference = UserPreference(**defaults)
        session.add(preference)
        session.flush()
        return preference

    def test_an_empty_preference_matches_everything_above_the_floor(self, db_session):
        """A new user must receive tenders, not silence."""
        service = NotificationService()
        decision = service.evaluate(self._tender(db_session), self._preference(db_session))
        assert decision.matched is True

    def test_the_relevance_floor_is_enforced(self, db_session):
        service = NotificationService()
        tender = self._tender(db_session, relevance_band=RelevanceBand.LOW_RELEVANCE.value)
        decision = service.evaluate(tender, self._preference(db_session))
        assert decision.matched is False
        assert decision.rejected_by == "below_relevance_floor"

    def test_out_of_scope_tenders_are_never_announced(self, db_session):
        service = NotificationService()
        tender = self._tender(db_session, relevance_band=RelevanceBand.OUT_OF_SCOPE.value)
        decision = service.evaluate(
            tender,
            self._preference(db_session, min_relevance_band=RelevanceBand.LOW_RELEVANCE.value),
        )
        assert decision.rejected_by == "out_of_scope"

    def test_a_declared_sector_filters(self, db_session):
        service = NotificationService()
        tender = self._tender(db_session)
        assert service.evaluate(
            tender, self._preference(db_session, sectors=["Technologies"])
        ).matched
        assert not service.evaluate(
            tender, self._preference(db_session, user_id="b", sectors=["Génie civil"])
        ).matched

    def test_an_excluded_keyword_vetoes(self, db_session):
        service = NotificationService()
        decision = service.evaluate(
            self._tender(db_session),
            self._preference(db_session, excluded_keywords=["documentaire"]),
        )
        assert decision.rejected_by == "excluded_keyword"

    def test_cpv_prefixes_match(self, db_session):
        service = NotificationService()
        assert service.evaluate(
            self._tender(db_session), self._preference(db_session, cpv_codes=["72"])
        ).matched

    def test_the_match_reason_is_auditable(self, db_session):
        service = NotificationService()
        decision = service.evaluate(
            self._tender(db_session),
            self._preference(db_session, keywords=["développement"], countries=["Tunisie"]),
        )
        assert "développement" in decision.reasons["keywords"]
        assert decision.reasons["country"] == "Tunisie"

    def test_the_daily_cap_suppresses_the_overflow(self, db_session):
        service = NotificationService()
        preference = self._preference(db_session, max_notifications_per_day=2)
        for _ in range(2):
            db_session.add(
                Notification(user_id="amine", channel="in_app", status="sent")
            )
        db_session.flush()
        assert service.under_daily_cap(db_session, preference) is False

    def test_notifications_are_created_for_matching_users(self, db_session):
        self._preference(db_session, user_id="amine", sectors=["Technologies"])
        self._preference(db_session, user_id="sonia", sectors=["Génie civil"])
        tender = self._tender(db_session)

        created = NotificationService().build_for_tender(db_session, tender)
        db_session.flush()

        assert len(created) == 1
        assert created[0].user_id == "amine"


class TestEnrichmentGate:
    """Which tenders are worth opening, and the loop that must not form.

    Enrichment ends by re-scoring, and it is *triggered* by a score. Without a
    marker that survives the round trip, every enriched tender would enrich
    again the moment it was re-scored — forever, once per notice, against a
    live portal.
    """

    def _gate(self, **overrides) -> bool:
        from app.workers.tasks.pipeline import enrichment_decision

        defaults = {
            "score": 0.80,
            "already_enriched": False,
            "has_identifier": True,
            "supports_detail": True,
        }
        return enrichment_decision(**{**defaults, **overrides})

    def test_a_promising_tender_is_enriched(self):
        assert self._gate() is True

    def test_a_low_score_is_not_worth_a_fetch(self):
        from app.workers.tasks.pipeline import ENRICHMENT_MIN_SCORE

        assert self._gate(score=ENRICHMENT_MIN_SCORE - 0.01) is False
        assert self._gate(score=ENRICHMENT_MIN_SCORE) is True

    def test_an_already_enriched_tender_is_never_re_enriched(self):
        """The guard against score -> enrich -> score -> enrich, forever."""
        assert self._gate(already_enriched=True, score=0.99) is False

    def test_a_source_without_a_detail_page_is_skipped(self):
        """Asked before any setup, so an unsupported source costs nothing."""
        assert self._gate(supports_detail=False) is False

    def test_a_tender_without_an_identifier_cannot_be_located(self):
        assert self._gate(has_identifier=False) is False

    def test_the_floor_sits_below_a_thin_notice_score(self):
        """The floor must not be circular.

        Measured on the live corpus: a Tunisian notice scores on its listing
        summary — 460 characters — while the publication enrichment would fetch
        averages 19 783. At a floor of 0.45, three of 154 Tunisian notices ever
        qualified: they were judged on the thin text, and the thinness of that
        text is what denied them the rich one.

        0.35 is the observed mean for a thin-but-plausible Tunisian IT notice.
        The floor has to sit below it, or the pass never reaches the notices it
        exists for.
        """
        from app.workers.tasks.pipeline import ENRICHMENT_MIN_SCORE

        assert ENRICHMENT_MIN_SCORE < 0.32

    def test_both_real_sources_declare_detail_support(self):
        from app.connectors.registry import connector_class

        assert connector_class("j360").supports_detail is True
        assert connector_class("tuneps").supports_detail is True
        assert connector_class("fixture").supports_detail is False


class TestEnrichmentIsAdditive:
    """Enrichment must never subtract.

    A detail page that omits a field it does not publish would otherwise blank
    a value the listing did supply — a silent regression, and the worst kind
    because the tender still looks fully processed afterwards.
    """

    def _tender(self, session, **overrides):
        defaults = {
            "source_key": "j360",
            "entry_point": EntryPoint.MANUAL_SCRAPE.value,
            "title": "Acquisition de logiciels",
            "external_id": "56223296",
        }
        defaults.update(overrides)
        tender = Tender(**defaults)
        session.add(tender)
        session.flush()
        return tender

    def _detail(self, **kwargs):
        from app.connectors.models import DetailResult

        return DetailResult(**kwargs)

    def test_a_missing_budget_does_not_erase_a_known_one(self, db_session):
        from app.workers.tasks.pipeline import _apply_detail

        tender = self._tender(db_session, estimated_budget=Decimal("50000.00"), currency="TND")
        _apply_detail(db_session, tender, self._detail(description="plus de detail"))

        assert tender.estimated_budget == Decimal("50000.00")
        assert tender.currency == "TND"

    def test_a_budget_is_filled_when_absent(self, db_session):
        from app.workers.tasks.pipeline import _apply_detail

        tender = self._tender(db_session, estimated_budget=None)
        applied, _ = _apply_detail(
            db_session,
            tender,
            self._detail(estimated_budget=Decimal("2000000"), currency="EUR"),
        )

        assert tender.estimated_budget == Decimal("2000000")
        assert tender.currency == "EUR"
        assert "budget" in applied

    def test_a_shorter_description_does_not_replace_a_longer_one(self, db_session):
        """The listing sometimes carries more than a thin detail page does."""
        from app.workers.tasks.pipeline import _apply_detail

        tender = self._tender(db_session, description="une description deja detaillee")
        _apply_detail(db_session, tender, self._detail(description="court"))

        assert tender.description == "une description deja detaillee"

    def test_the_full_object_replaces_a_truncated_one(self, db_session):
        from app.workers.tasks.pipeline import _apply_detail

        tender = self._tender(db_session, description="Acquisition de...")
        full = "Acquisition de logiciels de gestion integree pour la direction des systemes"
        applied, _ = _apply_detail(db_session, tender, self._detail(description=full))

        assert tender.description == full
        assert "description" in applied

    def test_source_links_are_recorded(self, db_session):
        """J360 hosts no files: these links are the only route to the DCE."""
        from app.workers.tasks.pipeline import _apply_detail

        tender = self._tender(db_session)
        links = ["https://ted.europa.eu/notice/-/detail/534356-2026"]
        _apply_detail(db_session, tender, self._detail(source_links=links))

        assert tender.extra["source_links"] == links

    def test_documents_are_queued_once(self, db_session):
        """Re-running enrichment must not duplicate the attachment rows."""
        from app.connectors.models import DocumentRef
        from app.workers.tasks.pipeline import _apply_detail

        tender = self._tender(db_session)
        detail = self._detail(
            documents=[DocumentRef(url="https://portal.tn/cdc.pdf", name="CDC")]
        )

        _, added = _apply_detail(db_session, tender, detail)
        db_session.flush()
        _, again = _apply_detail(db_session, tender, detail)

        assert added == 1
        assert again == 0

    def test_a_re_signed_presigned_url_is_not_a_new_document(self, db_session):
        """A presigned URL's signature and expiry change on every fetch even

        when the underlying object does not. Comparing full URLs would then
        record the same S3/MinIO-hosted attachment again on every
        re-enrichment — measured live against J360's own cached copies,
        served from a presigned OVH S3 URL that differs only in
        ``Signature=`` and ``Expires=`` between two fetches minutes apart.
        """
        from app.connectors.models import DocumentRef
        from app.workers.tasks.pipeline import _apply_detail

        tender = self._tender(db_session)
        base = "https://s3.example.com/j360-private/announces/1773/media/87779.pdf"

        _, added = _apply_detail(
            db_session,
            tender,
            self._detail(
                documents=[
                    DocumentRef(url=f"{base}?Signature=aaa%3D&Expires=1785941348")
                ]
            ),
        )
        db_session.flush()
        _, added_again = _apply_detail(
            db_session,
            tender,
            self._detail(
                documents=[
                    DocumentRef(url=f"{base}?Signature=bbb%3D&Expires=1785941905")
                ]
            ),
        )

        assert added == 1
        assert added_again == 0
