"""Contract tests for the job-match endpoint.

Same style as ``test_api.py``: the real FastAPI app against an in-memory async
database, with Celery's ``apply_async`` stubbed so these assert that the
endpoint validates input and **queues** work, never that the ranking itself
runs — that needs the embedding model and is exercised by
``test_matching.py`` and ``test_cv_profile.py`` instead. Fixtures are
duplicated from ``test_api.py`` rather than imported, to keep this file
additive and that one untouched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401
from app.api.deps import get_session
from app.db.base import Base
from app.main import create_app


class _StubTask:
    id = "stub-task-id"

    def get(self, timeout=None, propagate=True):
        return {"status": "ok", "candidates": []}


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    """Never publish to a real broker; record the calls and answer inline."""
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


class TestValidation:
    @pytest.mark.parametrize("data", [{"age_min": -1}, {"age_max": 121}, {"age_min": 40, "age_max": 20}, {"min_experience_years": -1}, {"min_experience_years": 61}, {"limit": -1}, {"limit": 201}])
    def test_invalid_filter_bounds(self, client, data):
        assert client.post("/job-match", data={"text": "Python developer", **data}).status_code == 422

    @pytest.mark.parametrize("owner,expected", [("amine", 200), (None, 200), ("other", 404)])
    def test_original_cv_link_checks_owner(self, client, monkeypatch, owner, expected):
        from types import SimpleNamespace
        from app.services import storage

        async def get_row(*args, **kwargs):
            return SimpleNamespace(uploaded_by=owner, storage_key="cvs/test.pdf", original_filename="test.pdf", content_type="application/pdf")

        monkeypatch.setattr(AsyncSession, "get", get_row)
        monkeypatch.setattr(storage, "get_storage", lambda: SimpleNamespace(presigned_url=lambda key: "https://storage.test/signed", presigned_ttl=900))
        response = client.get("/cvs/11111111-1111-1111-1111-111111111111/download")
        assert response.status_code == expected
        if expected == 200:
            assert response.json()["content_type"] == "application/pdf"
            assert response.json()["url"] == "https://storage.test/signed"

    def test_missing_cv_link(self, client):
        assert client.get("/cvs/11111111-1111-1111-1111-111111111111/download").status_code == 404

    def test_linkedin_import(self, client, monkeypatch):
        from app.services import linkedin_post

        async def imported(url):
            return {"text": "Python developer", "source_url": url}

        monkeypatch.setattr(linkedin_post, "import_linkedin_post", imported)
        response = client.post("/job-match/import-linkedin", data={"url": "https://www.linkedin.com/posts/test"})
        assert response.status_code == 200
        assert response.json()["text"] == "Python developer"

    def test_invalid_linkedin_import(self, client):
        response = client.post("/job-match/import-linkedin", data={"url": "https://localhost/"})
        assert response.status_code == 422

    def test_neither_text_nor_file_is_rejected(self, client):
        response = client.post("/job-match", data={})

        assert response.status_code == 400

    def test_blank_text_is_rejected(self, client):
        response = client.post("/job-match", data={"text": "   "})

        assert response.status_code == 400


class TestDispatch:
    def test_background_search_returns_without_waiting(self, client, monkeypatch):
        from unittest.mock import Mock
        from app.api.routers import job_match

        owners = Mock()
        monkeypatch.setattr(job_match, "_owners", lambda: owners)
        monkeypatch.setattr(_StubTask, "get", lambda *a, **kw: pytest.fail("Must not wait"))
        response = client.post("/job-match", data={"text": "Python developer", "background": "true"})
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        owners.setex.assert_called_once_with(
            f"job-match:{response.json()['task_id']}", 3600, "amine"
        )

    @pytest.mark.parametrize("state,expected", [("PENDING", "queued"), ("STARTED", "running"), ("SUCCESS", "completed")])
    def test_poll_search(self, client, monkeypatch, state, expected):
        from unittest.mock import Mock
        from app.api.routers import job_match
        from app.workers.celery_app import celery_app

        monkeypatch.setattr(job_match, "_owners", lambda: Mock(get=lambda key: "amine"))
        monkeypatch.setattr(celery_app, "AsyncResult", lambda task_id: Mock(state=state, result={"candidates": []}))
        response = client.get("/job-match/test")
        assert response.status_code == 200
        assert response.json()["status"] == expected
        if state == "SUCCESS":
            assert response.json()["result"] == {"candidates": []}

    def test_other_user_cannot_read_search(self, client, monkeypatch):
        from unittest.mock import Mock
        from app.api.routers import job_match

        monkeypatch.setattr(job_match, "_owners", lambda: Mock(get=lambda key: "another-user"))
        assert client.get("/job-match/test").status_code == 404

    def test_failed_search_returns_error(self, client, monkeypatch):
        from unittest.mock import Mock
        from app.api.routers import job_match
        from app.workers.celery_app import celery_app

        monkeypatch.setattr(job_match, "_owners", lambda: Mock(get=lambda key: "amine"))
        monkeypatch.setattr(celery_app, "AsyncResult", lambda task_id: Mock(state="FAILURE"))
        assert client.get("/job-match/test").status_code == 503

    def test_pasted_text_dispatches_the_ranking_task(self, client, _no_broker):
        response = client.post(
            "/job-match",
            data={
                "text": "Recherche developpeur Symfony experimente.",
                "age_min": "25",
                "certifications": "PMP, Prince2",
            },
        )

        assert response.status_code == 200
        assert len(_no_broker) == 1
        call = _no_broker[0]
        assert "rank_job_posting_candidates" in call["task"]
        assert call["kwargs"]["job_text"] == "Recherche developpeur Symfony experimente."
        assert call["kwargs"]["filters"]["age_min"] == 25
        assert call["kwargs"]["filters"]["certifications"] == ["PMP", "Prince2"]

    def test_default_filters_are_empty(self, client, _no_broker):
        client.post("/job-match", data={"text": "Recherche developpeur."})

        filters = _no_broker[0]["kwargs"]["filters"]
        assert filters["age_min"] is None
        assert filters["certifications"] == []

    def test_all_candidates_can_be_requested(self, client, _no_broker):
        response = client.post("/job-match", data={"text": "Python developer", "limit": "0"})
        assert response.status_code == 200
        assert _no_broker[0]["kwargs"]["limit"] == 0
