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
    def test_neither_text_nor_file_is_rejected(self, client):
        response = client.post("/job-match", data={})

        assert response.status_code == 400

    def test_blank_text_is_rejected(self, client):
        response = client.post("/job-match", data={"text": "   "})

        assert response.status_code == 400


class TestDispatch:
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
