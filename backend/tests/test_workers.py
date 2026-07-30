"""Celery layer: the Beat scheduler and the task bodies.

These were the two areas with no direct coverage. The scheduler in particular
is the most intricate component in the codebase — sentinel-driven reload,
overlap suppression, expiry — and all of it had only ever been reasoned about.

Tasks run eagerly against the in-memory database with ``apply_async`` stubbed,
which is the point rather than a compromise: the assertions are about *which*
task each stage hands off to, and letting them execute inline would hide
exactly that.
"""

from __future__ import annotations

import importlib
import uuid as uuid_module
from contextlib import contextmanager
from datetime import timedelta

import pytest

from app.core.enums import (
    JobStatus,
    NotificationChannel,
    NotificationStatus,
    RelevanceBand,
    SubmissionOutcome,
    TenderPipelineState,
)
from app.core.identity import utc_now
from app.db.models.job import ConnectorRun, ScrapingJob
from app.db.models.notification import Notification, UserPreference
from app.db.models.schedule import Schedule, ScheduleChangeSentinel
from app.db.models.submission import Submission
from app.db.models.tender import Tender

TASK_MODULES = (
    "app.workers.tasks.pipeline",
    "app.workers.tasks.notifications",
    "app.workers.tasks.maintenance",
    "app.workers.tasks.scraping",
    "app.workers.beat",
)


@pytest.fixture()
def dispatched(monkeypatch):
    """Capture every task publication instead of reaching a broker."""
    calls: list[dict] = []

    class _Result:
        id = "stub-task-id"

    def _apply_async(self, *args, **kwargs):
        calls.append({"task": self.name, "kwargs": kwargs.get("kwargs") or {}})
        return _Result()

    from celery.app.task import Task

    monkeypatch.setattr(Task, "apply_async", _apply_async, raising=False)
    return calls


@pytest.fixture()
def worker_db(db_session, monkeypatch):
    """Point every worker module's ``session_scope`` at the test session."""

    @contextmanager
    def _scope():
        yield db_session
        db_session.flush()

    for name in TASK_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "session_scope"):
            monkeypatch.setattr(module, "session_scope", _scope)
    return db_session


def _tender(session, **overrides) -> Tender:
    defaults = {
        "id": uuid_module.uuid4(),
        "source_key": "fixture",
        "entry_point": "manual_scrape",
        "title": "Développement d'une application de gestion documentaire",
        "description": "Développement applicatif et maintenance.",
        "buyer": "Ministère des Technologies de la Communication",
        "country": "Tunisie",
        "sector": "Technologies de l'information",
        "deadline": utc_now() + timedelta(days=25),
        "pipeline_state": TenderPipelineState.RECEIVED.value,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    defaults.update(overrides)
    tender = Tender(**defaults)
    session.add(tender)
    session.flush()
    return tender


# ===========================================================================
# Beat scheduler
# ===========================================================================
class TestModelEntry:
    def _schedule(self, **overrides) -> Schedule:
        defaults = {
            "id": uuid_module.uuid4(),
            "name": "test-schedule",
            "enabled": True,
            "kind": "interval",
            "interval_seconds": 3600,
            "connectors": ["fixture"],
            "filters": {},
            "timezone": "Africa/Tunis",
            "queue": "scraping",
            "skip_if_running": False,
            "one_off": False,
            "total_run_count": 0,
        }
        defaults.update(overrides)
        return Schedule(**defaults)

    def test_a_row_becomes_a_beat_entry(self):
        from app.workers.beat import ModelEntry

        entry = ModelEntry(self._schedule())
        assert entry.name == "test-schedule"
        assert entry.task == "app.workers.tasks.scraping.run_scraping_job"
        assert entry.kwargs["connectors"] == ["fixture"]
        assert entry.kwargs["trigger"] == "scheduled"
        assert entry.options["queue"] == "scraping"

    def test_a_disabled_schedule_is_never_due(self):
        from app.workers.beat import ModelEntry

        due, _ = ModelEntry(self._schedule(enabled=False)).is_due()
        assert due is False

    def test_a_schedule_is_due_once_its_interval_has_elapsed(self):
        from app.workers.beat import ModelEntry

        row = self._schedule(interval_seconds=60)
        row.last_run_at = utc_now() - timedelta(minutes=5)
        assert ModelEntry(row).is_due()[0] is True

    def test_a_schedule_is_not_due_before_its_interval(self):
        from app.workers.beat import ModelEntry

        row = self._schedule(interval_seconds=3600)
        row.last_run_at = utc_now() - timedelta(seconds=30)
        assert ModelEntry(row).is_due()[0] is False

    def test_start_after_defers_the_first_run(self):
        from app.workers.beat import ModelEntry

        row = self._schedule(start_after=utc_now() + timedelta(hours=2))
        row.last_run_at = utc_now() - timedelta(days=1)
        due, next_check = ModelEntry(row).is_due()
        assert due is False
        assert next_check > 0

    def test_an_expired_schedule_stops_firing(self):
        from app.workers.beat import ModelEntry

        row = self._schedule(expires_at=utc_now() - timedelta(minutes=1))
        row.last_run_at = utc_now() - timedelta(days=1)
        assert ModelEntry(row).is_due()[0] is False

    def test_firing_advances_the_run_bookkeeping(self):
        from app.workers.beat import ModelEntry

        row = self._schedule()
        entry = next(ModelEntry(row))

        assert row.total_run_count == 1
        assert row.last_run_at is not None
        assert entry.total_run_count == 1

    def test_a_one_off_schedule_disables_itself(self):
        from app.workers.beat import ModelEntry

        row = self._schedule(one_off=True)
        next(ModelEntry(row))
        assert row.enabled is False

    def test_a_crontab_schedule_builds(self):
        from app.workers.beat import ModelEntry

        entry = ModelEntry(
            self._schedule(
                kind="crontab",
                interval_seconds=None,
                cron_minute="0",
                cron_hour="7",
                cron_day_of_week="1-5",
            )
        )
        assert "crontab" in type(entry.schedule).__name__.lower()

    def test_overlap_suppression_skips_while_the_previous_job_runs(self, worker_db):
        """Otherwise a portal that got slow accumulates concurrent runs."""
        from app.workers.beat import ModelEntry

        job = ScrapingJob(id=uuid_module.uuid4(), status=JobStatus.RUNNING.value)
        worker_db.add(job)
        worker_db.flush()

        row = self._schedule(skip_if_running=True, last_job_id=job.id, interval_seconds=60)
        row.last_run_at = utc_now() - timedelta(minutes=10)

        due, next_check = ModelEntry(row).is_due()
        assert due is False
        # Re-checks soon rather than waiting a whole period, so the cadence
        # resumes the moment the running job finishes.
        assert next_check <= 60

    def test_overlap_suppression_allows_once_the_previous_job_finished(self, worker_db):
        from app.workers.beat import ModelEntry

        job = ScrapingJob(id=uuid_module.uuid4(), status=JobStatus.SUCCEEDED.value)
        worker_db.add(job)
        worker_db.flush()

        row = self._schedule(skip_if_running=True, last_job_id=job.id, interval_seconds=60)
        row.last_run_at = utc_now() - timedelta(minutes=10)
        assert ModelEntry(row).is_due()[0] is True


class TestDatabaseScheduler:
    def _scheduler(self, worker_db):
        from app.workers.beat import DatabaseScheduler
        from app.workers.celery_app import celery_app

        # lazy=True skips setup_schedule so the reload can be driven explicitly.
        return DatabaseScheduler(app=celery_app, lazy=True)

    def test_it_loads_only_enabled_schedules(self, worker_db):
        worker_db.add_all(
            [
                Schedule(name="on", kind="interval", interval_seconds=3600, enabled=True),
                Schedule(name="off", kind="interval", interval_seconds=3600, enabled=False),
            ]
        )
        worker_db.flush()

        scheduler = self._scheduler(worker_db)
        scheduler._reload()

        assert "on" in scheduler._schedule
        assert "off" not in scheduler._schedule

    def test_the_sentinel_signals_a_change(self, worker_db):
        scheduler = self._scheduler(worker_db)
        scheduler._reload()

        # Nothing changed since the reload.
        assert scheduler._sentinel_changed() is False

        worker_db.add(ScheduleChangeSentinel(id=1, last_update=utc_now()))
        worker_db.flush()
        assert scheduler._sentinel_changed() is True

        # ...and the change is consumed, not re-reported forever.
        assert scheduler._sentinel_changed() is False

    def test_a_new_schedule_is_picked_up_without_a_restart(self, worker_db):
        scheduler = self._scheduler(worker_db)
        scheduler._reload()
        assert scheduler._schedule == {}

        worker_db.add(Schedule(name="added-later", kind="interval", interval_seconds=3600))
        worker_db.add(ScheduleChangeSentinel(id=1, last_update=utc_now()))
        worker_db.flush()

        # This is what Beat does on its next tick.
        if scheduler._sentinel_changed():
            scheduler._reload()

        assert "added-later" in scheduler._schedule

    def test_it_hosts_the_static_maintenance_entries(self, worker_db):
        """Regression: Celery builds `beat_schedule` entries as
        ``Entry(name=..., task=..., schedule=...)``. A ModelEntry that only
        accepted a database row crashed Beat on startup, leaving the platform
        with no scheduler at all."""
        from app.workers.beat import ModelEntry
        from app.workers.celery_app import MAINTENANCE_SCHEDULE

        scheduler = self._scheduler(worker_db)
        scheduler.setup_schedule()

        for name in MAINTENANCE_SCHEDULE:
            assert name in scheduler._schedule, f"missing maintenance entry {name}"

        entry = scheduler._schedule["collect-queue-metrics"]
        assert isinstance(entry, ModelEntry)
        assert entry.model is None
        assert entry.task == "app.workers.tasks.maintenance.collect_queue_metrics"

    def test_a_static_entry_advances_without_touching_the_database(self, worker_db):
        from app.workers.beat import ModelEntry
        from app.workers.celery_app import celery_app

        entry = ModelEntry(
            app=celery_app,
            name="collect-queue-metrics",
            task="app.workers.tasks.maintenance.collect_queue_metrics",
            schedule=timedelta(seconds=30),
        )
        advanced = next(entry)

        assert advanced.total_run_count == 1
        assert advanced.model is None
        assert entry.is_due()[0] in (True, False)   # plain interval semantics

    def test_a_database_reload_does_not_drop_the_maintenance_entries(self, worker_db):
        """Otherwise the first schedule edit silently removes every
        reconciliation loop that keeps the pipeline self-healing."""
        scheduler = self._scheduler(worker_db)
        scheduler.setup_schedule()
        assert "reconcile-stuck-jobs" in scheduler._schedule

        worker_db.add(Schedule(name="added-later", kind="interval", interval_seconds=3600))
        worker_db.flush()
        scheduler._reload()

        assert "added-later" in scheduler._schedule
        assert "reconcile-stuck-jobs" in scheduler._schedule

    def test_syncing_ignores_static_entries(self, worker_db):
        scheduler = self._scheduler(worker_db)
        scheduler.setup_schedule()
        scheduler._dirty.add("collect-queue-metrics")

        scheduler.sync()   # must not raise on an entry with no schedule_id

    def test_a_reload_failure_keeps_serving_the_previous_schedule(
        self, worker_db, monkeypatch
    ):
        """A database blip must not make Beat go silent."""
        scheduler = self._scheduler(worker_db)
        worker_db.add(Schedule(name="keeper", kind="interval", interval_seconds=3600))
        worker_db.flush()
        scheduler._reload()
        assert "keeper" in scheduler._schedule

        import app.workers.beat as beat_module

        @contextmanager
        def _broken():
            raise RuntimeError("database is down")
            yield  # pragma: no cover

        monkeypatch.setattr(beat_module, "session_scope", _broken)
        scheduler._reload()

        assert "keeper" in scheduler._schedule


# ===========================================================================
# Pipeline tasks
# ===========================================================================
class TestPipelineTasks:
    def test_process_tender_with_no_documents_goes_straight_to_extraction(
        self, worker_db, dispatched
    ):
        from app.workers.tasks.pipeline import process_tender

        tender = _tender(worker_db)
        result = process_tender.run(tender_id=str(tender.id))

        assert result["status"] == "dispatched"
        assert any("extract_tender_text" in c["task"] for c in dispatched)
        # Scoring must not be dispatched yet — it reads the extracted text.
        assert not any("score_tender" in c["task"] for c in dispatched)

    def test_process_tender_is_idempotent_on_replay(self, worker_db, dispatched):
        from app.workers.tasks.pipeline import process_tender

        tender = _tender(worker_db, pipeline_state=TenderPipelineState.COMPLETED.value)
        result = process_tender.run(tender_id=str(tender.id))

        assert result["status"] == "already_completed"
        assert dispatched == []

    def test_a_missing_tender_is_not_an_error_worth_retrying(self, worker_db, dispatched):
        from app.workers.tasks.pipeline import process_tender

        result = process_tender.run(tender_id=str(uuid_module.uuid4()))
        assert result["status"] == "missing"

    def test_extraction_always_hands_off_to_scoring(self, worker_db, dispatched):
        """Even with nothing to extract — an extraction failure must never
        strand a tender unscored."""
        from app.workers.tasks.pipeline import extract_tender_text

        tender = _tender(worker_db)
        result = extract_tender_text.run(tender_id=str(tender.id))

        assert result["status"] == "nothing_to_extract"
        assert any("score_tender" in c["task"] for c in dispatched)

        worker_db.refresh(tender)
        assert tender.extraction_status == "empty"

    def test_extraction_survives_a_storage_failure(self, worker_db, dispatched, monkeypatch):
        from app.services import storage as storage_module
        from app.workers.tasks.pipeline import extract_tender_text

        class _BrokenStorage:
            def get_bytes(self, key):
                raise RuntimeError("MinIO unreachable")

        monkeypatch.setattr(storage_module, "get_storage", lambda: _BrokenStorage())

        tender = _tender(worker_db, storage_key="tenders/x/cdc.pdf", content_type="application/pdf")
        extract_tender_text.run(tender_id=str(tender.id))

        worker_db.refresh(tender)
        assert tender.extraction_status == "failed"
        assert "unreadable from storage" in (tender.extraction_error or "")
        # ...and scoring still happens.
        assert any("score_tender" in c["task"] for c in dispatched)

    def test_scoring_persists_the_score_and_notifies(self, worker_db, dispatched):
        from app.db.models.tender import TenderScore
        from app.workers.tasks.pipeline import score_tender

        tender = _tender(worker_db)
        result = score_tender.run(tender_id=str(tender.id))

        assert 0.0 <= result["score"] <= 1.0
        worker_db.refresh(tender)
        assert tender.relevance_score is not None
        assert tender.pipeline_state == TenderPipelineState.SCORED.value
        assert tender.scored_at is not None

        history = worker_db.query(TenderScore).filter_by(tender_id=tender.id).all()
        assert len(history) == 1
        assert history[0].breakdown

        assert any("dispatch_tender_notifications" in c["task"] for c in dispatched)

    def test_an_out_of_scope_tender_is_never_announced(self, worker_db, dispatched):
        from app.workers.tasks.pipeline import score_tender

        tender = _tender(
            worker_db,
            title="Travaux de génie civil pour la réhabilitation d'un entrepôt",
            description="Démolition, gros œuvre et charpente métallique.",
            sector="Bâtiment",
        )
        result = score_tender.run(tender_id=str(tender.id))

        assert result["band"] == RelevanceBand.OUT_OF_SCOPE.value
        assert not any("dispatch_tender_notifications" in c["task"] for c in dispatched)
        worker_db.refresh(tender)
        assert tender.pipeline_state == TenderPipelineState.COMPLETED.value

    def test_extracted_text_reaches_the_scorer(self, worker_db, dispatched):
        from app.workers.tasks.pipeline import score_tender

        bare = _tender(
            worker_db, title="AO 42/2026", description=None, sector=None
        )
        baseline = score_tender.run(tender_id=str(bare.id))["score"]

        rich = _tender(
            worker_db,
            title="AO 43/2026",
            description=None,
            sector=None,
            extracted_text=(
                "Développement d'applications web, intégration au système "
                "d'information, migration des données et tierce maintenance "
                "applicative. Consultant expert exigé."
            ),
        )
        improved = score_tender.run(tender_id=str(rich.id))["score"]

        assert improved > baseline


class TestHistoricalSuccessProvider:
    def _provider(self, session):
        from app.workers.tasks.pipeline import _history_provider

        return _history_provider(session)

    def test_no_history_makes_the_criterion_abstain(self, worker_db, make_tender):
        assert self._provider(worker_db)(make_tender()) is None

    def test_decided_submissions_produce_a_win_rate(self, worker_db, make_tender):
        buyer = "Ministère des Technologies de la Communication"
        worker_db.add_all(
            [
                Submission(buyer=buyer, outcome=SubmissionOutcome.WON.value),
                Submission(buyer=buyer, outcome=SubmissionOutcome.WON.value),
                Submission(buyer=buyer, outcome=SubmissionOutcome.LOST.value),
            ]
        )
        worker_db.flush()

        assert self._provider(worker_db)(make_tender(buyer=buyer)) == (2, 3)

    def test_pending_bids_are_excluded(self, worker_db, make_tender):
        """Awards are published months later; counting a pending bid as a loss
        would penalise every buyer with a slow procurement cycle."""
        buyer = "Ministère des Technologies de la Communication"
        worker_db.add_all(
            [
                Submission(buyer=buyer, outcome=SubmissionOutcome.WON.value),
                Submission(buyer=buyer, outcome=SubmissionOutcome.PENDING.value),
                Submission(buyer=buyer, outcome=SubmissionOutcome.PENDING.value),
            ]
        )
        worker_db.flush()

        assert self._provider(worker_db)(make_tender(buyer=buyer)) == (1, 1)

    def test_it_falls_back_to_the_sector(self, worker_db, make_tender):
        worker_db.add(
            Submission(
                buyer="Un autre acheteur",
                sector="Technologies de l'information",
                outcome=SubmissionOutcome.WON.value,
            )
        )
        worker_db.flush()

        tender = make_tender(buyer="Acheteur jamais rencontré")
        assert self._provider(worker_db)(tender) == (1, 1)


# ===========================================================================
# Notification tasks and digests
# ===========================================================================
class TestNotificationTasks:
    def _preference(self, session, **overrides) -> UserPreference:
        defaults = {
            "user_id": "amine",
            "email": "amine@example.tn",
            "active": True,
            "channels": ["in_app", "email"],
            "min_relevance_band": RelevanceBand.RELEVANT.value,
            "digest_frequency": "immediate",
        }
        defaults.update(overrides)
        preference = UserPreference(**defaults)
        session.add(preference)
        session.flush()
        return preference

    def _scored(self, session) -> Tender:
        return _tender(
            session,
            relevance_score=0.85,
            relevance_band=RelevanceBand.HIGHLY_RELEVANT.value,
        )

    def test_dispatch_creates_and_delivers(self, worker_db, dispatched):
        from app.workers.tasks.notifications import dispatch_tender_notifications

        self._preference(worker_db)
        tender = self._scored(worker_db)

        result = dispatch_tender_notifications.run(tender_id=str(tender.id))

        assert result["created"] >= 1
        assert any("deliver_notification" in c["task"] for c in dispatched)
        worker_db.refresh(tender)
        assert tender.pipeline_state == TenderPipelineState.COMPLETED.value

    def test_a_replay_does_not_double_notify(self, worker_db, dispatched):
        from app.workers.tasks.notifications import dispatch_tender_notifications

        self._preference(worker_db)
        tender = self._scored(worker_db)

        dispatch_tender_notifications.run(tender_id=str(tender.id))
        second = dispatch_tender_notifications.run(tender_id=str(tender.id))

        assert second["status"] == "already_dispatched"

    def test_in_app_delivery_marks_sent(self, worker_db, dispatched):
        from app.workers.tasks.notifications import deliver_notification

        notification = Notification(
            user_id="amine",
            channel=NotificationChannel.IN_APP.value,
            status=NotificationStatus.PENDING.value,
            subject="x",
        )
        worker_db.add(notification)
        worker_db.flush()

        result = deliver_notification.run(notification_id=str(notification.id))
        assert result["status"] == NotificationStatus.SENT.value

    def test_a_digest_batches_the_period(self, worker_db, dispatched, monkeypatch):
        from app.services.notifications import NotificationService
        from app.workers.tasks.notifications import send_digests

        monkeypatch.setattr(NotificationService, "email_enabled", True, raising=False)
        self._preference(worker_db, digest_frequency="daily", channels=["in_app"])

        for index in range(3):
            worker_db.add(
                Notification(
                    user_id="amine",
                    tender_id=self._scored(worker_db).id,
                    channel=NotificationChannel.IN_APP.value,
                    status=NotificationStatus.SENT.value,
                    subject=f"Tender {index}",
                    payload={"title": f"Tender {index}", "score": 0.8, "band": "relevant"},
                    created_at=utc_now(),
                )
            )
        worker_db.flush()

        result = send_digests.run(frequency="daily")
        assert result["digests"] == 1

        digest = (
            worker_db.query(Notification)
            .filter(
                Notification.channel == NotificationChannel.EMAIL.value,
                Notification.tender_id.is_(None),
            )
            .one()
        )
        assert "3" in digest.subject
        assert "Tender 0" in digest.body
        assert digest.payload["kind"] == "digest"

    def test_an_empty_window_produces_no_digest(self, worker_db, dispatched, monkeypatch):
        """An empty digest is worse than silence."""
        from app.services.notifications import NotificationService
        from app.workers.tasks.notifications import send_digests

        monkeypatch.setattr(NotificationService, "email_enabled", True, raising=False)
        self._preference(worker_db, digest_frequency="daily")

        assert send_digests.run(frequency="daily")["digests"] == 0

    def test_a_digest_is_not_sent_twice_for_the_same_window(
        self, worker_db, dispatched, monkeypatch
    ):
        from app.services.notifications import NotificationService
        from app.workers.tasks.notifications import send_digests

        monkeypatch.setattr(NotificationService, "email_enabled", True, raising=False)
        self._preference(worker_db, digest_frequency="daily", channels=["in_app"])
        worker_db.add(
            Notification(
                user_id="amine",
                tender_id=self._scored(worker_db).id,
                channel=NotificationChannel.IN_APP.value,
                status=NotificationStatus.SENT.value,
                payload={"title": "T"},
                created_at=utc_now(),
            )
        )
        worker_db.flush()

        assert send_digests.run(frequency="daily")["digests"] == 1
        assert send_digests.run(frequency="daily")["digests"] == 0

    def test_immediate_subscribers_are_not_digested(self, worker_db, dispatched, monkeypatch):
        from app.services.notifications import NotificationService
        from app.workers.tasks.notifications import send_digests

        monkeypatch.setattr(NotificationService, "email_enabled", True, raising=False)
        self._preference(worker_db, digest_frequency="immediate")
        worker_db.add(
            Notification(
                user_id="amine",
                tender_id=self._scored(worker_db).id,
                channel=NotificationChannel.IN_APP.value,
                status=NotificationStatus.SENT.value,
                payload={"title": "T"},
                created_at=utc_now(),
            )
        )
        worker_db.flush()

        assert send_digests.run(frequency="daily")["digests"] == 0


# ===========================================================================
# Maintenance tasks
# ===========================================================================
class TestMaintenanceTasks:
    def test_stuck_jobs_are_closed_out(self, worker_db, dispatched):
        """A killed worker otherwise leaves a job RUNNING forever, which blocks
        its schedule's overlap guard permanently."""
        from app.workers.tasks.maintenance import reconcile_stuck_jobs

        old = utc_now() - timedelta(hours=4)
        job = ScrapingJob(
            id=uuid_module.uuid4(), status=JobStatus.RUNNING.value, created_at=old
        )
        worker_db.add(job)
        worker_db.flush()
        worker_db.add(
            ConnectorRun(
                id=uuid_module.uuid4(),
                job_id=job.id,
                connector_key="fixture",
                status=JobStatus.RUNNING.value,
                created_at=old,
            )
        )
        worker_db.flush()

        result = reconcile_stuck_jobs.run(timeout_minutes=90)

        assert result["runs_closed"] == 1
        worker_db.refresh(job)
        assert job.status in {JobStatus.TIMED_OUT.value, JobStatus.FAILED.value}

    def test_a_recent_job_is_left_alone(self, worker_db, dispatched):
        from app.workers.tasks.maintenance import reconcile_stuck_jobs

        job = ScrapingJob(
            id=uuid_module.uuid4(),
            status=JobStatus.RUNNING.value,
            created_at=utc_now(),
        )
        worker_db.add(job)
        worker_db.flush()

        reconcile_stuck_jobs.run(timeout_minutes=90)
        worker_db.refresh(job)
        assert job.status == JobStatus.RUNNING.value

    def test_stalled_tenders_are_requeued(self, worker_db, dispatched):
        from app.workers.tasks.maintenance import requeue_stalled_tenders

        _tender(
            worker_db,
            pipeline_state=TenderPipelineState.RECEIVED.value,
            created_at=utc_now() - timedelta(hours=2),
        )

        result = requeue_stalled_tenders.run(older_than_minutes=30)

        assert result["requeued"] == 1
        assert any("process_tender" in c["task"] for c in dispatched)

    def test_a_completed_tender_is_not_requeued(self, worker_db, dispatched):
        from app.workers.tasks.maintenance import requeue_stalled_tenders

        _tender(
            worker_db,
            pipeline_state=TenderPipelineState.COMPLETED.value,
            created_at=utc_now() - timedelta(hours=2),
        )
        assert requeue_stalled_tenders.run(older_than_minutes=30)["requeued"] == 0

    def test_connector_health_flags_a_silent_source(self, worker_db, dispatched):
        """Silence is the dangerous failure: a source that simply stopped
        running produces no error at all."""
        from app.db.models.source import Source
        from app.workers.tasks.maintenance import check_connector_health

        worker_db.add(
            Source(
                key="fixture",
                name="Fixture",
                enabled=True,
                health="healthy",
                last_run_at=utc_now() - timedelta(days=3),
            )
        )
        worker_db.flush()

        result = check_connector_health.run(silence_hours=26)
        assert any(a["type"] == "silent" for a in result["alerts"])

    def test_old_logs_are_purged(self, worker_db, dispatched):
        from app.db.models.log import ExecutionLog
        from app.workers.tasks.maintenance import purge_old_logs

        worker_db.add_all(
            [
                ExecutionLog(
                    event="old.event", level="INFO", ts=utc_now() - timedelta(days=400)
                ),
                ExecutionLog(event="recent.event", level="INFO", ts=utc_now()),
            ]
        )
        worker_db.flush()

        assert purge_old_logs.run(retention_days=180)["purged"] == 1
        assert worker_db.query(ExecutionLog).count() == 1
