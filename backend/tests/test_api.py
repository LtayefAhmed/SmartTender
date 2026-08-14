"""API contract tests.

Runs the real FastAPI application against an in-memory async database, with
Celery's ``apply_async`` stubbed. Stubbing the broker is the point rather than a
compromise: these tests assert that the endpoints **queue** work and return
immediately, which is precisely the behaviour that would be hidden by letting
tasks execute inline.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401
from app.api.deps import get_session
from app.core.enums import EntryPoint, RelevanceBand, TenderStatus
from app.core.identity import utc_now
from app.db.base import Base
from app.db.models.tender import Tender
from app.main import create_app


class _StubTask:
    id = "stub-task-id"


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    """Never publish to a real broker; record the calls instead."""
    published: list[dict] = []

    def _apply_async(self, *args, **kwargs):
        published.append({"task": self.name, "kwargs": kwargs.get("kwargs", {})})
        return _StubTask()

    from celery.app.task import Task

    monkeypatch.setattr(Task, "apply_async", _apply_async, raising=False)
    return published


@pytest_asyncio.fixture()
async def async_engine() -> AsyncIterator:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def client(async_engine) -> AsyncIterator[TestClient]:
    factory = async_sessionmaker(bind=async_engine, expire_on_commit=False, autoflush=False)

    async def _override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_session] = _override
    with TestClient(app) as test_client:
        test_client.headers.update({"X-API-Key": "test-key", "X-User-Id": "amine"})
        yield test_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture()
async def seeded(async_engine):
    """Two tenders of differing relevance."""
    factory = async_sessionmaker(bind=async_engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Tender(
                    id=uuid_module.uuid4(),
                    source_key="tuneps",
                    entry_point=EntryPoint.SCHEDULED_SCRAPE.value,
                    title="Développement d'une plateforme de gestion documentaire",
                    buyer="Ministère des Technologies de la Communication",
                    country="Tunisie",
                    sector="Technologies de l'information",
                    status=TenderStatus.OPEN.value,
                    relevance_score=0.88,
                    relevance_band=RelevanceBand.HIGHLY_RELEVANT.value,
                    deadline=utc_now() + timedelta(days=20),
                    estimated_budget=Decimal("1250000"),
                    currency="TND",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                ),
                Tender(
                    id=uuid_module.uuid4(),
                    source_key="j360",
                    entry_point=EntryPoint.MANUAL_SCRAPE.value,
                    title="Fourniture de mobilier de bureau",
                    buyer="Commune de Sfax",
                    country="Tunisie",
                    status=TenderStatus.OPEN.value,
                    relevance_score=0.31,
                    relevance_band=RelevanceBand.LOW_RELEVANCE.value,
                    deadline=utc_now() + timedelta(days=40),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                ),
            ]
        )
        await session.commit()


# ---------------------------------------------------------------------------
class TestHealth:
    def test_liveness_never_depends_on_infrastructure(self, client):
        """A database outage must not make Kubernetes restart every API pod."""
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    def test_metrics_are_exposed_in_prometheus_format(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "build_info" in response.text

    def test_root_advertises_the_docs(self, client):
        assert client.get("/").json()["docs"] == "/docs"


class TestAuthentication:
    def test_a_missing_key_is_refused(self, client):
        response = client.get("/tenders", headers={"X-API-Key": ""})
        assert response.status_code == 401

    def test_a_wrong_key_is_refused(self, client):
        response = client.get("/tenders", headers={"X-API-Key": "nope"})
        assert response.status_code == 401


class TestScrapeEndpoint:
    def test_it_returns_202_without_waiting(self, client, _no_broker):
        response = client.post("/scrape", json={"connectors": ["fixture"], "filters": {}})
        assert response.status_code == 202

        body = response.json()
        assert body["accepted"] is True
        assert body["job_id"]
        assert body["poll_url"] == f"/scrape/jobs/{body['job_id']}"
        # Queued, not executed.
        assert any("run_scraping_job" in call["task"] for call in _no_broker)

    def test_filters_are_persisted_with_the_job(self, client):
        response = client.post(
            "/scrape",
            json={
                "connectors": ["fixture"],
                "filters": {
                    "keywords": ["développement", "maintenance"],
                    "countries": ["Tunisie"],
                    "budget_min": 100000,
                    "published_within_days": 30,
                },
            },
        )
        job_id = response.json()["job_id"]

        job = client.get(f"/scrape/jobs/{job_id}").json()
        assert job["filters"]["keywords"] == ["développement", "maintenance"]
        # Relative shorthands are resolved at launch so a replay is identical.
        assert job["filters"]["publication_date_from"]

    def test_an_unavailable_connector_is_a_clear_400(self, client):
        response = client.post("/scrape", json={"connectors": ["j360"]})
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "j360" in detail["skipped"]
        assert "available" in detail

    def test_an_unknown_connector_is_reported(self, client):
        response = client.post("/scrape", json={"connectors": ["does-not-exist"]})
        assert response.status_code == 400

    def test_invalid_filters_are_rejected_with_field_detail(self, client):
        response = client.post(
            "/scrape",
            json={"filters": {"budget_min": 5000, "budget_max": 100}},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "request_validation_error"

    def test_unknown_filter_keys_are_refused(self, client):
        response = client.post("/scrape", json={"filters": {"not_a_filter": 1}})
        assert response.status_code == 422

    def test_a_missing_job_is_404(self, client):
        assert client.get(f"/scrape/jobs/{uuid_module.uuid4()}").status_code == 404

    def test_jobs_can_be_listed(self, client):
        client.post("/scrape", json={"connectors": ["fixture"]})
        body = client.get("/scrape/jobs").json()
        assert body["total"] >= 1
        assert body["items"][0]["progress"] == 0.0


class TestUploadEndpoint:
    def test_a_valid_pdf_is_accepted(self, client, minimal_pdf, monkeypatch):
        from app.services import ingestion as ingestion_module
        from app.services.storage import StoredObject

        class _FakeStorage:
            def build_key(self, tender_id, filename, **kwargs):
                return f"tenders/{tender_id}/{filename}"

            def put_bytes(self, key, data, **kwargs):
                return StoredObject(
                    bucket="test", key=key, size_bytes=len(data), content_type="application/pdf"
                )

        monkeypatch.setattr(ingestion_module, "get_storage", lambda: _FakeStorage())

        response = client.post(
            "/upload",
            files={"file": ("cahier des charges.pdf", minimal_pdf, "application/pdf")},
            data={"title": "Appel d'offres pour le développement d'une application"},
        )
        assert response.status_code == 202
        assert response.json()["tender_id"]

    def test_an_executable_is_refused_with_an_explicit_reason(self, client):
        response = client.post(
            "/upload",
            files={"file": ("payload.pdf", b"MZ\x90\x00" + b"\x00" * 300, "application/pdf")},
            data={"title": "Un titre valide pour ce document"},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "suspicious_content"
        assert "executable" in body["message"]

    def test_html_with_a_script_is_refused(self, client):
        response = client.post(
            "/upload",
            files={"file": ("avis.html", b"<html><script>x()</script></html>", "text/html")},
            data={"title": "Un titre valide pour ce document"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "suspicious_content"

    def test_a_disallowed_extension_is_refused(self, client):
        response = client.post(
            "/upload",
            files={"file": ("notes.txt", b"just text", "text/plain")},
            data={"title": "Un titre valide"},
        )
        assert response.status_code == 415
        assert response.json()["code"] == "unsupported_media_type"


class TestTenderEndpoints:
    def test_listing_is_paginated(self, client, seeded):
        body = client.get("/tenders", params={"page": 1, "page_size": 1}).json()
        assert body["total"] == 2
        assert len(body["items"]) == 1

    def test_default_sort_is_by_relevance(self, client, seeded):
        items = client.get("/tenders").json()["items"]
        assert items[0]["relevance_score"] > items[1]["relevance_score"]

    def test_free_text_search(self, client, seeded):
        body = client.get("/tenders", params={"q": "documentaire"}).json()
        assert body["total"] == 1

    def test_band_filter(self, client, seeded):
        body = client.get("/tenders", params={"bands": ["highly_relevant"]}).json()
        assert body["total"] == 1

    def test_min_score_filter(self, client, seeded):
        assert client.get("/tenders", params={"min_score": 0.5}).json()["total"] == 1

    def test_connector_filter(self, client, seeded):
        assert client.get("/tenders", params={"connectors": ["j360"]}).json()["total"] == 1

    def test_an_unsortable_field_is_refused(self, client, seeded):
        response = client.get("/tenders", params={"sort": "; DROP TABLE tenders"})
        assert response.status_code == 400

    def test_urgency_is_computed_for_the_dashboard(self, client, seeded):
        items = client.get("/tenders").json()["items"]
        assert items[0]["days_until_deadline"] is not None
        assert items[0]["is_urgent"] is False

    def test_detail_of_a_missing_tender_is_404(self, client):
        assert client.get(f"/tenders/{uuid_module.uuid4()}").status_code == 404

    def test_download_without_a_stored_document_is_404(self, client, seeded):
        tender_id = client.get("/tenders").json()["items"][0]["id"]
        assert client.get(f"/tenders/{tender_id}/download").status_code == 404

    def test_dashboard_counters(self, client, seeded):
        body = client.get("/tenders/stats/overview").json()
        assert body["total_tenders"] == 2
        assert body["by_source"]["tuneps"] == 1
        assert "highly_relevant" in body["band_metadata"]


class TestScheduleEndpoints:
    def test_presets_are_advertised(self, client):
        keys = [p["key"] for p in client.get("/schedules/presets").json()["presets"]]
        for expected in ("hourly", "every_2_hours", "every_6_hours", "daily", "weekly"):
            assert expected in keys

    def test_a_preset_creates_an_interval_schedule(self, client):
        response = client.post(
            "/schedules",
            json={"name": "tuneps-hourly", "preset": "hourly", "connectors": ["fixture"]},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["interval_seconds"] == 3600
        assert body["cadence"] == "every hour"

    def test_an_explicit_interval_works(self, client):
        response = client.post(
            "/schedules",
            json={"name": "every-2h", "interval_seconds": 7200, "connectors": ["fixture"]},
        )
        assert response.json()["cadence"] == "every 2 hours"

    def test_a_crontab_schedule(self, client):
        response = client.post(
            "/schedules",
            json={
                "name": "weekdays-7am",
                "kind": "crontab",
                "cron_minute": "0",
                "cron_hour": "7",
                "cron_day_of_week": "1-5",
            },
        )
        assert response.status_code == 201
        assert "cron(" in response.json()["cadence"]

    def test_a_schedule_with_no_cadence_is_refused(self, client):
        response = client.post("/schedules", json={"name": "broken"})
        assert response.status_code == 422

    def test_an_unknown_preset_is_refused(self, client):
        response = client.post("/schedules", json={"name": "x", "preset": "every_leap_year"})
        assert response.status_code == 422

    def test_an_unknown_connector_is_refused(self, client):
        response = client.post(
            "/schedules",
            json={"name": "x", "preset": "daily", "connectors": ["nope"]},
        )
        assert response.status_code == 400

    def test_duplicate_names_are_refused(self, client):
        client.post("/schedules", json={"name": "dup", "preset": "daily"})
        response = client.post("/schedules", json={"name": "dup", "preset": "daily"})
        assert response.status_code == 409

    def test_a_schedule_can_be_edited_and_toggled(self, client):
        created = client.post("/schedules", json={"name": "editable", "preset": "daily"}).json()
        schedule_id = created["id"]

        updated = client.put(
            f"/schedules/{schedule_id}", json={"preset": "every_6_hours"}
        ).json()
        assert updated["interval_seconds"] == 21600

        toggled = client.post(f"/schedules/{schedule_id}/toggle").json()
        assert toggled["enabled"] is False

    def test_running_a_schedule_now_queues_a_job(self, client, _no_broker):
        created = client.post("/schedules", json={"name": "runnable", "preset": "daily"}).json()
        response = client.post(f"/schedules/{created['id']}/run")
        assert response.status_code == 202
        assert response.json()["job_id"]

    def test_a_schedule_can_be_deleted(self, client):
        created = client.post("/schedules", json={"name": "temp", "preset": "daily"}).json()
        assert client.delete(f"/schedules/{created['id']}").status_code == 204
        assert client.get(f"/schedules/{created['id']}").status_code == 404


class TestSourceEndpoints:
    def test_the_registry_explains_why_a_source_is_unavailable(self, client):
        body = client.get("/sources/registry").json()
        by_key = {c["key"]: c for c in body["connectors"]}

        # TUNEPS is a public listing — available with no credentials.
        assert by_key["tuneps"]["available"] is True
        assert by_key["tuneps"]["requires_credentials"] is False

        # J360 needs a paid subscription, and the reason must name the exact
        # variables to set rather than just the category.
        assert by_key["j360"]["available"] is False
        assert by_key["j360"]["unavailable_reason"] == "credentials_missing"
        assert by_key["j360"]["missing_credentials"]

        assert by_key["fixture"]["available"] is True
        assert "fixture" in body["available"]

    def test_an_unknown_source_is_404(self, client):
        assert client.get("/sources/nope").status_code == 404


class TestPreferences:
    def test_preferences_round_trip(self, client):
        payload = {
            "email": "amine@example.tn",
            "sectors": ["Technologies de l'information"],
            "countries": ["Tunisie"],
            "min_relevance_band": "relevant",
            "channels": ["in_app"],
        }
        assert client.put("/preferences", json=payload).status_code == 200

        stored = client.get("/preferences").json()
        assert stored["user_id"] == "amine"
        assert stored["sectors"] == ["Technologies de l'information"]

    def test_missing_preferences_are_404(self, client):
        assert client.get("/preferences").status_code == 404


class TestAdminEndpoints:
    def test_the_scoring_profile_is_exposed(self, client):
        body = client.get("/admin/scoring/profile").json()
        assert body["weights"]["field_of_work"] > 0
        assert "highly_relevant" in body["bands"]

    def test_rescoring_is_queued_not_executed(self, client, _no_broker):
        response = client.post("/admin/scoring/rescore", params={"limit": 10})
        assert response.status_code == 202
        assert any("rescore_all" in call["task"] for call in _no_broker)

    def test_duplicates_can_be_inspected(self, client):
        assert client.get("/admin/duplicates").json()["total"] == 0

    def test_the_audit_trail_is_queryable(self, client):
        assert client.get("/admin/logs").status_code == 200


class TestErrorContract:
    def test_every_error_carries_a_stable_code_and_request_id(self, client):
        response = client.get(f"/tenders/{uuid_module.uuid4()}")
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "http_404"
        assert body["request_id"]
        assert response.headers["X-Request-ID"] == body["request_id"]

    def test_security_headers_are_present(self, client):
        response = client.get("/health/live")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"


class TestBusinessDomains:
    """The domain picker is served from the scoring profile, not duplicated.

    One definition means adding a domain to config/scoring.yaml makes it both
    scoreable and searchable — the alternative is a frontend list that drifts
    from the scorer until a domain is searchable but scores nothing.
    """

    def test_it_serves_the_seven_inetum_domains(self, client):
        body = client.get("/admin/business-domains").json()
        names = [d["name"] for d in body["domains"]]

        assert names == [
            "SAGE",
            "SIRH",
            "SAP",
            "Microsoft",
            "Application Services",
            "Digital consulting",
            "IA & Data",
        ]

    def test_each_domain_carries_both_vocabularies(self, client):
        """Expertise terms score; search terms are what portals understand.
        A domain missing either is half-wired."""
        body = client.get("/admin/business-domains").json()

        for domain in body["domains"]:
            assert domain["expertise"], f"{domain['name']} has no scoring terms"
            assert domain["search_terms"], f"{domain['name']} has no search terms"

    def test_search_terms_speak_the_buyers_language(self, client):
        """Vendor names belong to scoring. Searching TUNEPS for "SAGE X3"
        returns nothing — a Tunisian notice says "progiciel de gestion"."""
        body = client.get("/admin/business-domains").json()
        sage = next(d for d in body["domains"] if d["name"] == "SAGE")

        assert "SAGE X3" in sage["expertise"]
        assert any("progiciel" in t.lower() for t in sage["search_terms"])

    def test_the_profile_version_is_reported(self, client):
        """The picker's vocabulary and a stored score must be traceable to the
        same profile revision."""
        assert client.get("/admin/business-domains").json()["profile_version"]


class TestAttachmentDownload:
    """A stored attachment must be reachable from the interface.

    Fifteen files were fetched for one tender, five were dropped by a cap, and
    the ten that arrived could only be read by opening the object store by
    hand. Collecting a cahier des charges nobody can open is close to not
    collecting it.
    """

    def _tender_with_document(self, client, **doc):
        from app.db.models.tender import Tender, TenderDocument
        from app.db.session import session_scope

        with session_scope() as session:
            tender = Tender(
                source_key="j360",
                entry_point=EntryPoint.MANUAL_SCRAPE.value,
                title="Renouvellement de la TMA",
            )
            session.add(tender)
            session.flush()
            document = TenderDocument(tender_id=tender.id, **doc)
            session.add(document)
            session.flush()
            return str(tender.id), str(document.id)

    def test_a_missing_attachment_is_a_404(self, client):
        import uuid as uuid_module

        response = client.get(
            f"/tenders/{uuid_module.uuid4()}/documents/{uuid_module.uuid4()}/download"
        )
        assert response.status_code == 404


class TestAttachmentPriority:
    """When a cap binds, it must keep the documents a bid depends on.

    Measured on a real consultation: a ceiling of ten dropped the règlement de
    consultation, the CCTP and the software-stack annex — the three that matter
    — while keeping ATTRI and DC1 forms a bidder merely fills in.
    """

    def test_substantive_documents_are_fetched_first(self):
        from app.workers.tasks.pipeline import _by_importance

        pending = [
            ("1", "u", "CNSO_ATTRI1.doc"),
            ("2", "u", "CNSO_RC_VF.pdf"),
            ("3", "u", "CNSO_DC1.doc"),
            ("4", "u", "CNSO_CCTP_VF.pdf"),
            ("5", "u", "CNSO_PAS RGPD modele.docx"),
        ]
        ordered = [name for _, _, name in _by_importance(pending)]

        assert ordered[0].startswith("CNSO_CCTP")
        assert "CNSO_RC_VF.pdf" in ordered[:2]
        assert ordered[-1].startswith("CNSO_PAS")

    def test_the_cap_now_exceeds_a_real_consultation(self):
        """Fifteen files is an ordinary French tender, not an outlier."""
        from app.workers.tasks.pipeline import _by_importance

        assert len(_by_importance([(str(i), "u", f"f{i}.pdf") for i in range(15)])) == 15
