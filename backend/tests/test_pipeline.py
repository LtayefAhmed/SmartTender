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
