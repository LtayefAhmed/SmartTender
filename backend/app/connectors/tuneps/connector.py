"""TUNEPS connector — public listing, rendered from an Angular single-page app.

The "Avis A.O" listing at ``/portail/offres`` is **publicly readable**: no
TUNTRUST certificate is needed to browse it (a certificate is only required to
submit a bid). The page is an Angular app that fetches an encrypted API and
renders a Material table client-side, so there is neither static HTML to scrape
nor a stable JSON endpoint to call. This connector renders the page in a real
browser and reads the table the app produces, driving the paginator to walk
through the pages.

Almost everything is in ``config/connectors/tuneps.yaml``. The code here covers
what is genuinely SPA-specific:

* pagination is a button click, not a URL, so ``fetch`` drives the Material
  paginator rather than the base class's URL-based paging;
* the listing gives the fields that matter for detection — reference, buyer,
  title, publication date, deadline, and the portal's own record id — but not
  budget/sector/CPV, which live on the detail page (a later enrichment pass);
* Arabic-only titles are common, and the country is implicit.

When TUNEPS restyles its table the fix is a selector edit in the YAML. The
``cdk-column-*`` classes are tied to the app's data model rather than to
styling, so they are unusually durable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.connectors.generic.html_connector import HtmlListingConnector
from app.connectors.models import FetchedPage, NormalizedTender, RawRecord
from app.connectors.registry import register
from app.core.enums import TenderStatus
from app.core.identity import utc_now
from app.schemas.filters import TenderFilters

__all__ = ["TunepsConnector"]


@register("tuneps")
class TunepsConnector(HtmlListingConnector):
    """Tunisian public procurement portal (Angular SPA, public listing)."""

    async def setup(self) -> None:
        from app.connectors.browser.playwright_client import BrowserRenderer

        # No HTTP client and no credentials: this connector only ever renders a
        # public page in a browser.
        self.http = None
        self._browser = BrowserRenderer(
            connector_key=self.key,
            config=self.config.http,
            allow_private_hosts=self.context.allow_private_hosts,
            allow_insecure_tls=self.config.allow_insecure_tls,
        )

    async def teardown(self) -> None:
        if getattr(self, "_browser", None) is not None:
            await self._browser.aclose()
            self._browser = None

    async def fetch(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        """Render the listing and walk the SPA paginator.

        Filters are recorded for the run report and then applied client-side by
        the base class (``matches_filters``): the portal's own filter form is an
        Angular reactive form whose controls are far more brittle to drive than
        the listing is to read, and the listing is small and newest-first.
        """
        self.build_query(filters)  # populates the filter-application report

        selectors = self.config.selectors
        paginator = selectors.get("paginator") or {}
        url = self.config.base_url.rstrip("/") + "/" + self.endpoint("search").lstrip("/")

        async for page in self._browser.render_paginated(
            url,
            rows_selector=paginator.get("rows_present") or selectors["list_item"],
            next_button_selector=paginator.get("next_button")
            or "button.mat-paginator-navigation-next",
            max_pages=self.max_pages,
            settle_ms=1500,
        ):
            if self.out_of_time:
                self.log.warning("connector.pagination_deadline")
                return
            self.note_page()
            yield page

    def parse(self, page: FetchedPage) -> list[RawRecord]:
        """Parse the rendered table, giving each row a stable, unique URL.

        The listing rows carry no detail hyperlink (navigation is a JS click),
        so the base parser would stamp every row on a page with the same page
        URL — and in-run deduplication would then collapse a whole page of
        distinct tenders into one. Each record instead gets a synthetic detail
        URL derived from the portal's own record id, which is also what a later
        enrichment pass will use to open the detail view.
        """
        records = super().parse(page)
        for record in records:
            identifier = record.get("external_id") or record.get("reference")
            if identifier:
                record.source_url = (
                    f"{self.config.base_url.rstrip('/')}"
                    f"/portail/offres/details?epBidMasterId={identifier}"
                )
        return records

    def normalize(self, record: RawRecord) -> NormalizedTender:
        tender = super().normalize(record)

        # Single-country portal: the country is never written on the page.
        tender.country = tender.country or "Tunisie"

        # The reference (bidNo, e.g. "20260701931") is stable; keep the portal's
        # internal id as the strongest dedup/enrichment key.
        external_id = record.get("external_id")
        if external_id:
            tender.external_id = external_id
            # The detail route is /portail/offres/details?...; record enough to
            # reconstruct it in a later enrichment pass.
            tender.extra["epBidMasterId"] = external_id

        # The listing carries no explicit status. Deriving it from the deadline
        # is accurate for the only distinction that matters operationally — can
        # we still bid — and is flagged as derived.
        if tender.status is TenderStatus.UNKNOWN and tender.deadline is not None:
            tender.status = (
                TenderStatus.OPEN if tender.deadline > utc_now() else TenderStatus.CLOSED
            )
            tender.extra["status_derived_from"] = "deadline"

        # Titles are frequently Arabic-only; that is legitimate content, so no
        # special handling is needed beyond what normalisation already does.
        return tender
