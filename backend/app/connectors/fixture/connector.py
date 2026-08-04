"""Fixture connector — serves saved pages from disk.

It exists for two reasons:

1. **CI can exercise the entire pipeline.** Scraping job → parse → validate →
   dedup → store → score → notify runs end to end with no network, no flakiness
   and byte-identical output every time. Tests that depend on a live portal are
   tests that fail on the day the portal has a bad afternoon.
2. **It is the reference implementation.** A developer adding a source starts
   here: it is the smallest complete connector in the codebase.

It refuses to run outside development and test environments.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from app.connectors.generic.html_connector import HtmlListingConnector
from app.connectors.models import FetchedPage, RawRecord
from app.connectors.registry import register
from app.core.config import BACKEND_ROOT
from app.core.exceptions import SourceUnavailableError
from app.core.security import ensure_within
from app.schemas.filters import TenderFilters

__all__ = ["FixtureConnector"]


@register("fixture")
class FixtureConnector(HtmlListingConnector):
    """Reads listing and detail pages from ``fixtures_dir`` instead of HTTP."""

    @classmethod
    def fixtures_dir(cls, config) -> Path:
        return (BACKEND_ROOT / (config.get("fixtures_dir") or "tests/fixtures/pages")).resolve()

    @classmethod
    def unmet_precondition(cls, config) -> str | None:
        """The sample pages live under ``tests/``, which runtime images exclude.

        Without this the connector advertises itself, is offered in the source
        picker, and then fails on every run — so a demonstration fixture ends up
        looking to the client like a broken production source.
        """
        if not cls.fixtures_dir(config).is_dir():
            return "fixtures_unavailable"
        return None

    async def setup(self) -> None:
        # No HTTP client and no browser: this connector never touches the
        # network, which is the entire point.
        self.http = None
        self._browser = None
        configured = self.config.get("fixtures_dir") or "tests/fixtures/pages"
        self._fixtures_dir = (BACKEND_ROOT / configured).resolve()
        if not self._fixtures_dir.is_dir():
            raise SourceUnavailableError(
                "Fixture directory does not exist.",
                connector=self.key,
                context={"path": str(self._fixtures_dir)},
            )

    async def teardown(self) -> None:
        return None

    def _read(self, relative: str) -> FetchedPage:
        # ensure_within is not theatre here: a fixture path is assembled from
        # config and from hrefs inside the fixture HTML, so it is exactly the
        # shape of input that produces traversal bugs.
        target = ensure_within(self._fixtures_dir, Path(relative.lstrip("/")))
        if not target.is_file():
            raise SourceUnavailableError(
                "Fixture page not found.",
                connector=self.key,
                context={"path": str(target)},
            )
        return FetchedPage(
            url=target.as_uri(),
            status_code=200,
            content=target.read_bytes(),
            headers={"content-type": "text/html; charset=utf-8"},
            encoding="utf-8",
        )

    async def fetch(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        self.build_query(filters)  # record filter application for the report
        template = self.endpoint("search")
        start = int(self.config.pagination.get("start_page") or 1)

        for offset in range(self.max_pages):
            page_number = start + offset
            try:
                page = self._read(template.format(page=page_number))
            except SourceUnavailableError:
                # Running out of fixture pages is the normal end of pagination.
                return
            self.note_page()
            yield page

    async def enrich(self, record: RawRecord) -> RawRecord:
        detail_selectors = self.config.selectors.get("detail")
        if not detail_selectors or not record.source_url:
            return record

        name = Path(record.source_url).name
        try:
            page = self._read(name)
        except SourceUnavailableError:
            return record

        from app.connectors.parsing.selectors import SelectorEngine, parse_html

        engine = SelectorEngine(parse_html(page.content))
        multi = {"cpv_codes", "documents", "document_names"}
        for field, value in engine.extract(detail_selectors, multi=multi).items():
            if value not in (None, [], ""):
                record.fields.setdefault(field, value)
        self.note_page()
        return record
