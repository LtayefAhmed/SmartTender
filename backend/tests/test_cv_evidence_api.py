from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.test_job_match_api import client, async_engine  # noqa: F401


@pytest.mark.parametrize("owner,content_type,expected", [
    ("other-user", "application/pdf", 404),
    ("amine", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", 415),
])
def test_access_and_document_type(client, monkeypatch, owner, content_type, expected):
    monkeypatch.setattr(AsyncSession, "get", AsyncMock(return_value=SimpleNamespace(uploaded_by=owner, content_type=content_type)))
    assert client.post(f"/cvs/{uuid.uuid4()}/evidence", json={"query": "Python"}).status_code == expected


def test_missing_cv(client, monkeypatch):
    monkeypatch.setattr(AsyncSession, "get", AsyncMock(return_value=None))
    assert client.post(f"/cvs/{uuid.uuid4()}/evidence", json={"query": "Python"}).status_code == 404


def test_authorized_real_pdf_process(client, monkeypatch):
    from tests.test_pdf_evidence import pdf
    from app.services import storage
    monkeypatch.setattr(AsyncSession, "get", AsyncMock(return_value=SimpleNamespace(uploaded_by="amine", content_type="application/pdf", size_bytes=1000, storage_key="test")))
    monkeypatch.setattr(storage, "get_storage", lambda: SimpleNamespace(get_bytes=lambda key: pdf()))
    response = client.post(f"/cvs/{uuid.uuid4()}/evidence", json={"query": "Python"})
    assert response.status_code == 200, response.text
    assert response.json()["pages"][0]["page"] == 2
