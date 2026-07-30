"""Generic HTML listing connector.

Drives the pattern nearly every procurement portal follows: a paginated search
listing whose rows carry a few fields and a link to a detail page. Everything
that varies — endpoint, query parameters, pagination style, selectors, date
formats, decimal separator — comes from the connector's YAML.

The three pagination modes are all bounded, and all three stop early: on an
empty page, on a page whose rows we have already seen, and on the configured
page ceiling. An incremental run against a portal that published nothing costs
exactly one request.

Static and dynamic fetching share this class; the only difference is whether a
page comes from httpx or from Playwright, which is decided by the ``strategy``
key and handled in ``_load_page``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode, urljoin

from app.connectors.base import BaseConnector
from app.connectors.models import FetchedPage, NormalizedTender, RawRecord
from app.connectors.parsing.normalizers import (
    normalize_date,
    normalize_email,
    normalize_money,
    normalize_text,
    strip_patterns,
)
from app.connectors.parsing.selectors import SelectorEngine, parse_html
from app.core.enums import FetchStrategy, ProcurementType, TenderStatus, coerce
from app.core.exceptions import ParsingError
from app.core.identity import canonicalize_url
from app.core.security import redact_url
from app.schemas.filters import FilterApplication, TenderFilters

__all__ = ["HtmlListingConnector"]


class HtmlListingConnector(BaseConnector):
    """Paginated HTML search listing, optionally enriched from detail pages."""

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    async def setup(self) -> None:
        from app.connectors.http.client import ResilientHttpClient

        self.http = ResilientHttpClient(
            connector_key=self.key,
            config=self.config.http,
            base_url=self.config.base_url,
            allow_private_hosts=self.context.allow_private_hosts,
            client_cert=self.config.client_certificate(),
        )
        self._browser: Any = None
        if self.config.strategy is FetchStrategy.DYNAMIC:
            from app.connectors.browser.playwright_client import BrowserRenderer

            self._browser = BrowserRenderer(
                connector_key=self.key,
                config=self.config.http,
                allow_private_hosts=self.context.allow_private_hosts,
            )

    async def teardown(self) -> None:
        if getattr(self, "_browser", None) is not None:
            await self._browser.aclose()
            self._browser = None

    # ------------------------------------------------------------------
    # Filter translation
    # ------------------------------------------------------------------
    def build_query(self, filters: TenderFilters) -> dict[str, Any]:
        """Translate the canonical filters into this portal's query parameters.

        Anything the portal cannot express is recorded as client-side so the
        operator can see how the search was actually executed, then applied
        after normalisation by ``matches_filters``.
        """
        mapping = self.config.filter_mapping
        values = self.config.filter_values
        query: dict[str, Any] = {}
        application = FilterApplication()

        def push(canonical: str, value: Any, *, enum_group: str | None = None) -> None:
            if value in (None, [], ""):
                return
            param = mapping.get(canonical)
            if not param:
                application.client_side.append(canonical)
                return
            if enum_group and enum_group in values:
                lookup = values[enum_group]
                if isinstance(value, list):
                    value = [lookup.get(str(v), str(v)) for v in value]
                else:
                    value = lookup.get(str(value), str(value))
            if isinstance(value, list):
                value = ",".join(str(v) for v in value)
            query[param] = value
            application.server_side.append(canonical)

        push("keywords", filters.keywords)
        push("country", filters.countries)
        push("organization", filters.organizations)
        push("ministry", filters.ministries)
        push("funding_organization", filters.funding_organizations)
        push("sector", filters.sectors)
        push("procurement_category", filters.procurement_categories)
        push("location", filters.locations)
        push("cpv_codes", filters.cpv_codes)
        push("language", filters.languages)
        push("document_type", filters.document_types)
        push(
            "procurement_type",
            [t.value for t in filters.procurement_types],
            enum_group="procurement_type",
        )
        push("status", [s.value for s in filters.statuses], enum_group="status")
        push(
            "publication_date_from",
            filters.publication_date_from.isoformat() if filters.publication_date_from else None,
        )
        push(
            "publication_date_to",
            filters.publication_date_to.isoformat() if filters.publication_date_to else None,
        )
        push("deadline_from", filters.deadline_from.isoformat() if filters.deadline_from else None)
        push("deadline_to", filters.deadline_to.isoformat() if filters.deadline_to else None)
        push("budget_min", filters.budget_min)
        push("budget_max", filters.budget_max)

        self._filter_application = application
        return query

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------
    async def _load_page(self, url: str) -> FetchedPage:
        if getattr(self, "_browser", None) is not None:
            wait_for = self.config.selectors.get("list_container")
            actions = self.config.get("browser_actions") or []
            page = await self._browser.render(url, wait_for_selector=wait_for, actions=actions)
        else:
            assert self.http is not None
            page = await self.http.get(url)
        self.note_page()
        return page

    def _page_url(self, base_path: str, query: dict[str, Any], page_number: int) -> str:
        pagination = self.config.pagination
        mode = str(pagination.get("mode") or "query").lower()
        params = dict(query)

        if mode == "path":
            path = base_path.format(page=page_number, **params)
            return urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))

        if pagination.get("page_size_param") and pagination.get("page_size"):
            params[str(pagination["page_size_param"])] = pagination["page_size"]
        if mode == "query":
            params[str(pagination.get("page_param") or "page")] = page_number

        path = base_path
        url = urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))
        return f"{url}?{urlencode(params, doseq=True)}" if params else url

    async def fetch(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        query = self.build_query(filters)
        search_path = self.endpoint("search")
        pagination = self.config.pagination
        start_page = int(pagination.get("start_page") or 1)
        stop_on_empty = bool(pagination.get("stop_on_empty_page", True))
        stop_after_known = int(pagination.get("stop_after_consecutive_known") or 0)

        consecutive_known = 0
        seen_signatures: set[str] = set()

        for offset in range(self.max_pages):
            if self.out_of_time:
                self.log.warning("connector.pagination_deadline", pages=offset)
                return

            page_number = start_page + offset
            url = self._page_url(search_path, query, page_number)
            page = await self._load_page(url)

            # Peek at the row count to decide whether to keep paginating. Doing
            # it here (rather than in the caller) is what makes "stop as soon as
            # there is nothing new" cheap.
            signatures = self._row_signatures(page)
            if not signatures and stop_on_empty:
                self.log.info("connector.pagination_stopped", reason="empty_page", page=page_number)
                yield page
                return

            if stop_after_known and signatures and signatures <= seen_signatures:
                consecutive_known += 1
                if consecutive_known >= stop_after_known:
                    self.log.info(
                        "connector.pagination_stopped",
                        reason="no_new_items",
                        page=page_number,
                    )
                    return
            else:
                consecutive_known = 0
            seen_signatures |= signatures

            yield page

    def _row_signatures(self, page: FetchedPage) -> set[str]:
        """Cheap identity of the rows on a page, for the early-stop check."""
        try:
            engine = SelectorEngine(parse_html(page.content))
            item_selector = self.config.selectors.get("list_item")
            item_fields = self.config.selectors.get("item") or {}
            signatures = set()
            for row in engine.nodes(item_selector):
                identity = row.get(item_fields.get("detail_url")) or row.get(
                    item_fields.get("reference")
                ) or row.text[:120]
                if identity:
                    signatures.add(identity)
            return signatures
        except Exception:
            return set()

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------
    def parse(self, page: FetchedPage) -> list[RawRecord]:
        soup = parse_html(page.content)
        engine = SelectorEngine(soup)
        selectors = self.config.selectors

        # An explicit "no results" marker means the page is legitimately empty
        # and the guard selector below must not fire.
        if selectors.get("no_results") and engine.exists(selectors["no_results"]):
            return []

        container = selectors.get("list_container")
        if container:
            engine.require(container, what="the results listing", url=page.url)

        item_selector = selectors.get("list_item")
        if not item_selector:
            raise ParsingError(
                "Connector config defines no 'list_item' selector.",
                connector=self.key,
                url=page.url,
            )

        item_fields: dict[str, str] = selectors.get("item") or {}
        multi_fields = {"cpv_codes", "documents", "document_names"}

        records: list[RawRecord] = []
        for row in engine.nodes(item_selector):
            fields = row.extract(item_fields, multi=multi_fields)
            detail = fields.get("detail_url")
            source_url = self._absolute(detail, page.url) if detail else page.url
            records.append(
                RawRecord(
                    connector_key=self.key,
                    source_url=source_url,
                    fields={k: v for k, v in fields.items() if v not in (None, [], "")},
                    page_number=None,
                )
            )
        return records

    def _absolute(self, url: str | None, base: str) -> str:
        if not url:
            return base
        if url.startswith(("http://", "https://")):
            return url
        return urljoin(base, url)

    # ------------------------------------------------------------------
    # Enrich
    # ------------------------------------------------------------------
    async def enrich(self, record: RawRecord) -> RawRecord:
        """Fetch and merge the detail page when the config defines one."""
        detail_selectors = self.config.selectors.get("detail")
        if not detail_selectors or not record.source_url:
            return record
        if record.source_url == self.config.base_url or self.out_of_time:
            return record

        page = await self._load_page(record.source_url)
        engine = SelectorEngine(parse_html(page.content))
        multi_fields = {"cpv_codes", "documents", "document_names"}
        detail_fields = engine.extract(detail_selectors, multi=multi_fields)

        # The listing is authoritative for anything it provided: detail pages
        # sometimes render a truncated title or a localised date.
        for name, value in detail_fields.items():
            if value in (None, [], ""):
                continue
            record.fields.setdefault(name, value)
            if name in multi_fields:
                record.fields[name] = value

        urls = detail_fields.get("documents") or []
        names = detail_fields.get("document_names") or []
        record.documents = [
            {
                "url": self._absolute(url, page.url),
                "name": names[index] if index < len(names) else None,
            }
            for index, url in enumerate(urls)
        ]

        if self.config.get("archive_original", False):
            record.raw_content = page.content
            record.content_type = page.headers.get("content-type", "text/html")
        return record

    # ------------------------------------------------------------------
    # Normalize
    # ------------------------------------------------------------------
    def normalize(self, record: RawRecord) -> NormalizedTender:
        parsing = self.config.parsing
        formats = list(parsing.get("date_formats") or [])
        tz = self.config.get("timezone")
        patterns = list(parsing.get("strip_patterns") or [])

        def text(name: str, max_length: int | None = None) -> str | None:
            return strip_patterns(normalize_text(record.get(name), max_length=max_length), patterns)

        def when(name: str):
            return normalize_date(record.get(name), formats=formats, tz=tz)

        amount, currency = normalize_money(
            record.get("estimated_budget"),
            decimal_separator=str(parsing.get("decimal_separator") or ","),
            thousands_separator=str(parsing.get("thousands_separator") or " "),
            default_currency=parsing.get("default_currency"),
        )

        title = text("title", 1024)
        if not title:
            raise ParsingError(
                "Record has no usable title after normalisation.",
                connector=self.key,
                url=redact_url(record.source_url),
            )

        from app.connectors.models import DocumentRef

        documents = [
            DocumentRef(url=doc["url"], name=doc.get("name"))
            for doc in record.documents
            if doc.get("url")
        ]

        strip_params = (
            (self.config.get("dedup") or {}).get("strip_query_params") or []
        )

        return NormalizedTender(
            connector_key=self.key,
            source_url=record.source_url,
            canonical_url=canonicalize_url(record.source_url, strip_params=strip_params),
            external_id=text("external_id"),
            reference=text("reference", 255),
            title=title,
            description=text("description"),
            buyer=text("buyer", 512),
            funding_organization=text("funding_organization", 512),
            contact_email=normalize_email(record.get("contact_email")),
            language=self.config.language,
            country=text("country") or self.config.country,
            location=text("location", 255),
            sector=text("sector", 255),
            category=text("category", 255),
            cpv_codes=record.get("cpv_codes") or [],
            procurement_type=coerce(
                ProcurementType,
                self._reverse_enum("procurement_type", record.get("procurement_type")),
                ProcurementType.UNKNOWN,
            ),
            status=coerce(
                TenderStatus,
                self._reverse_enum("status", record.get("status")),
                TenderStatus.UNKNOWN,
            ),
            publication_date=when("publication_date"),
            deadline=when("deadline"),
            estimated_budget=amount,
            currency=currency,
            documents=documents,
        )

    def _reverse_enum(self, group: str, value: Any) -> Any:
        """Map a portal-specific code back to the canonical vocabulary."""
        if value is None:
            return None
        table = self.config.filter_values.get(group) or {}
        text_value = str(value).strip()
        for canonical, portal_value in table.items():
            if str(portal_value).lower() == text_value.lower():
                return canonical
        return text_value
