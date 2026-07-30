"""Shared test configuration.

The whole suite runs with **no infrastructure**: no PostgreSQL, no Redis, no
MinIO, no network. That is a deliberate constraint, not a shortcut — a test
suite that needs docker-compose is a suite people stop running, and a suite
that reaches a live portal fails whenever that portal has a bad afternoon.

The seams that make it possible were designed in from the start:

* every model type has a SQLite variant, so the real ORM runs in memory;
* parsers take bytes, so they run against saved fixtures;
* the similarity backend is deterministic, so dedup asserts exact values;
* the fixture connector reads from disk, so the pipeline runs end to end.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# Environment must be set before any application module reads settings.
os.environ.setdefault("SMARTTENDER_ENV", "test")
os.environ.setdefault("SMARTTENDER_LOG_LEVEL", "WARNING")
os.environ.setdefault("SMARTTENDER_LOG_FORMAT", "console")
os.environ.setdefault("SMARTTENDER_SEMANTIC__BACKEND", "lexical")
os.environ.setdefault("SMARTTENDER_API__API_KEYS", '["test-key"]')

# Captured browser sessions are read from an empty directory, never from the
# developer's real `certs/`. Otherwise "J360 is unavailable without a session"
# starts passing or failing depending on who last signed in — and the suite
# stops being evidence of anything.
import tempfile as _tempfile

os.environ["SMARTTENDER_SESSION_DIR"] = _tempfile.mkdtemp(prefix="smarttender-sessions-")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.db.models  # noqa: F401  (registers every mapper)
from app.connectors.models import DocumentRef, NormalizedTender
from app.core.config import BACKEND_ROOT, reset_settings_cache
from app.core.enums import ProcurementType, TenderStatus
from app.core.identity import utc_now
from app.db.base import Base

FIXTURES = BACKEND_ROOT / "tests" / "fixtures"
PAGES = FIXTURES / "pages"


@pytest.fixture(scope="session", autouse=True)
def _settings() -> Iterator[None]:
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture()
def engine():
    """In-memory SQLite engine holding the real schema."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture()
def db_session(engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def page_bytes():
    """Read a saved page fixture."""

    def _read(name: str) -> bytes:
        return (PAGES / name).read_bytes()

    return _read


@pytest.fixture()
def make_tender():
    """Build a NormalizedTender with sensible, overridable defaults."""

    def _make(**overrides) -> NormalizedTender:
        defaults = {
            "connector_key": "fixture",
            "source_url": "https://portal.example.tn/avis/1",
            "canonical_url": "https://portal.example.tn/avis/1",
            "reference": "AO 01/2026",
            "title": "Développement d'une application web de gestion",
            "description": (
                "Conception et développement d'une application web, intégration au "
                "système d'information et tierce maintenance applicative."
            ),
            "buyer": "Ministère des Technologies de la Communication",
            "country": "Tunisie",
            "location": "Tunis",
            "sector": "Technologies de l'information",
            "cpv_codes": ["72200000"],
            "procurement_type": ProcurementType.OPEN,
            "status": TenderStatus.OPEN,
            "publication_date": utc_now() - timedelta(days=2),
            "deadline": utc_now() + timedelta(days=25),
            "estimated_budget": Decimal("850000.00"),
            "currency": "TND",
            "documents": [DocumentRef(url="https://portal.example.tn/cdc.pdf", name="CDC")],
        }
        defaults.update(overrides)
        return NormalizedTender(**defaults)

    return _make


@pytest.fixture()
def minimal_pdf() -> bytes:
    """The smallest structurally valid PDF the validator will accept."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"%%EOF\n"
    )


@pytest.fixture()
def minimal_docx() -> bytes:
    """A structurally valid DOCX (a ZIP with the required parts)."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.'
            'org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Cahier des charges'
            "</w:t></w:r></w:p></w:body></w:document>",
        )
    return buffer.getvalue()


@pytest.fixture()
def macro_docx() -> bytes:
    """A DOCX carrying a VBA project — must be refused."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
        archive.writestr("word/vbaProject.bin", b"\x00\x01macro payload")
    return buffer.getvalue()


@pytest.fixture()
def fixtures_dir() -> Path:
    return PAGES
