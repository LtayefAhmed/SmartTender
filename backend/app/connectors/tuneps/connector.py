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

import re
from collections.abc import AsyncIterator
from typing import Any

from app.connectors.generic.html_connector import HtmlListingConnector
from app.connectors.models import (
    DetailResult,
    FetchedPage,
    NormalizedTender,
    RawRecord,
    TenderDetailRequest,
)
from app.connectors.registry import register
from app.core.enums import TenderStatus
from app.core.identity import utc_now
from app.schemas.filters import FilterApplication, TenderFilters

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
        """Drive the portal's own search form, then walk the SPA paginator.

        Filling the form matters more than it looks: TUNEPS publishes thousands
        of notices across every sector, and the overwhelming majority are public
        works. Filtering locally means rendering page after page of road
        resurfacing to find one software tender. Typing the same term into the
        portal's "Objet A.O" field cuts the result set to the matches before a
        single row is parsed.
        """
        # OR across terms, against a form with a single free-text field: run one
        # search per term rather than pushing none. Pushing none means paging
        # the portal's entire catalogue and discarding it locally — thousands of
        # public-works notices read to find a handful of software ones. Each
        # search here returns matches, and in-run deduplication collapses a
        # notice found by several terms.
        # One term is one term: OR and AND describe the same request, so the
        # single-keyword case takes the ordinary path and is pushed to the form.
        if filters.keywords_any and len(filters.keywords) > 1:
            async for page in self._fetch_per_keyword(filters):
                yield page
            return

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
            actions=self._search_actions(filters),
        ):
            if self.out_of_time:
                self.log.warning("connector.pagination_deadline")
                return
            self.note_page()
            yield page

    async def _fetch_per_keyword(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        """One form submission per term, because the field holds only one.

        The base class stops as soon as enough results have accumulated, so a
        common term usually satisfies the quota before the rarer ones are asked
        for — the cost stays close to a single search rather than multiplying by
        the number of terms.
        """
        selectors = self.config.selectors
        paginator = selectors.get("paginator") or {}
        url = self.config.base_url.rstrip("/") + "/" + self.endpoint("search").lstrip("/")
        terms = list(filters.keywords)
        self.log.info("connector.or_search", terms=len(terms))

        for index, term in enumerate(terms, start=1):
            if self.out_of_time:
                self.log.warning("connector.deadline_reached_between_terms", done=index - 1)
                return

            single = filters.model_copy(update={"keywords": [term], "keywords_any": False})
            actions = self._search_actions(single)
            # Report the union: the portal honoured the keyword, once per search.
            self._filter_application.server_side = sorted(
                set(self._filter_application.server_side) | {"keywords"}
            )

            async for page in self._browser.render_paginated(
                url,
                rows_selector=paginator.get("rows_present") or selectors["list_item"],
                next_button_selector=paginator.get("next_button")
                or "button.mat-paginator-navigation-next",
                max_pages=self.max_pages,
                settle_ms=1500,
                actions=actions,
            ):
                if self.out_of_time:
                    self.log.warning("connector.pagination_deadline")
                    return
                self.note_page()
                yield page

    def _search_actions(self, filters: TenderFilters) -> list[dict[str, Any]]:
        """Translate canonical filters into interactions with the portal's form.

        The form offers a single free-text field ("Objet A.O"), which forces one
        judgement call:

        **A single keyword can only be pushed when every keyword must match.**
        With AND semantics (the default) any one term must appear in every
        result, so sending one narrows the set without losing anything — the
        rest are then applied locally. With OR semantics (``keywords_any``)
        sending one term would *exclude* tenders matching only the others, which
        would quietly return wrong results. So in that case nothing is pushed
        and all the filtering stays local.
        """
        form = (self.config.selectors.get("search_form") or {})
        paginator = (self.config.selectors.get("paginator") or {})
        application = FilterApplication()
        actions: list[dict[str, Any]] = []

        pushed_keyword: str | None = None
        if filters.keywords and form.get("objet"):
            if filters.keywords_any and len(filters.keywords) > 1:
                # See docstring: pushing one of several alternatives would drop
                # valid matches. A lone term has no alternatives to lose.
                application.client_side.append("keywords")
            else:
                pushed_keyword = filters.keywords[0]
                application.server_side.append("keywords")
                if len(filters.keywords) > 1:
                    application.client_side.append("keywords")
        elif filters.keywords:
            application.client_side.append("keywords")

        # Search first. Submitting resets the paginator, so asking for a larger
        # page before searching would have the size thrown away.
        if pushed_keyword and form.get("submit"):
            actions += [
                {
                    "type": "fill",
                    "selector": form["objet"],
                    "value": pushed_keyword,
                    # Required: a search field that silently stayed empty would
                    # return the entire catalogue, and the run would report
                    # those results as matching the user's criteria.
                    "required": True,
                },
                {"type": "click", "selector": form["submit"], "required": True},
                {"type": "wait", "ms": 3000},
            ]

        # Then ask for more rows per page. Purely an efficiency setting: it
        # changes how much work both sides do, never which tenders come back.
        # Not required — a page-size control that moved costs us extra page
        # loads, which is a waste, not a wrong answer.
        size_select = paginator.get("page_size_select")
        rows_per_page = self.config.pagination.get("rows_per_page")
        if size_select and rows_per_page and int(rows_per_page) > 10:
            actions += [
                {
                    "type": "material_select",
                    "selector": size_select,
                    "value": str(rows_per_page),
                },
                {"type": "wait", "ms": 2500},
            ]

        # Criteria the form cannot express are applied after normalisation.
        for canonical, value in (
            ("countries", filters.countries),
            ("sectors", filters.sectors),
            ("organizations", filters.organizations),
            ("excluded_keywords", filters.excluded_keywords),
            ("budget_min", filters.budget_min),
            ("deadline_from", filters.deadline_from),
        ):
            if value:
                application.client_side.append(canonical)

        self._filter_application = application
        if pushed_keyword:
            self.log.info("connector.search_form_applied", keyword=pushed_keyword)
        return actions

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
        base = self.config.base_url.rstrip("/")
        for record in records:
            identifier = record.get("external_id")
            reference = record.get("reference")
            # Read off the portal by clicking a row: the detail view is a
            # two-segment PATH, /details/<epBidMasterId>/<N° A.O> — not a query
            # parameter. A guessed `?epBidMasterId=` URL returns 404, which is
            # worse than no link at all: it looks like the tender vanished.
            if identifier and reference:
                record.source_url = f"{base}/portail/offres/details/{identifier}/{reference}"
            elif identifier:
                record.source_url = f"{base}/portail/offres/details/{identifier}"
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

    # ------------------------------------------------------------------
    # Detail enrichment
    # ------------------------------------------------------------------
    supports_detail = True

    async def fetch_detail(self, tender: TenderDetailRequest) -> DetailResult | None:
        """Render the notice's own "Fiche détails" page and read its fields.

        The listing truncates the object to a table cell. This page carries it
        in full, along with the procurement procedure, the lots, the evaluation
        method and the contact — everything scoring needs to do more than match
        a title, and the only description a CV can be compared against.

        Attachments are deliberately not sought: downloading a cahier des
        charges on TUNEPS requires a TUNTRUST certificate, which is a *bidding*
        credential. Reading is public; taking the file is not.
        """
        if not (tender.external_id and tender.reference):
            return None

        detail_config = (self.config.selectors.get("detail") or {})
        labels: dict[str, str] = detail_config.get("labels") or {}
        if not labels:
            return None

        url = (
            f"{self.config.base_url.rstrip('/')}/portail/offres/details/"
            f"{tender.external_id}/{tender.reference}"
        )
        page = await self._browser.render(
            url,
            wait_for_selector=detail_config.get("wait_for"),
            extra_wait_ms=3000,
        )

        values = self._read_labelled_values(page.text, labels)
        if not values:
            # The page rendered but carried none of the expected labels, which
            # means the layout moved rather than that the notice is thin.
            self.log.warning(
                "connector.detail_labels_missing",
                url=url,
                expected=sorted(labels.values())[:5],
            )
            return None

        result = DetailResult()
        result.description = values.pop("description", None)
        result.buyer = values.pop("buyer", None)
        contact = values.pop("contact_name", None)

        # Translate the portal's wording into the platform vocabulary here,
        # where the mapping lives beside the source it belongs to. An unmapped
        # procedure still travels — in `extra` — rather than being dropped.
        procedure = values.pop("procurement_type", None)
        if procedure:
            mapping = (self.config.filter_values.get("procurement_type") or {})
            result.procurement_type = mapping.get(procedure.strip(), procedure)
            values["procurement_procedure"] = procedure
        result.extra = {k: v for k, v in values.items() if v}
        if contact:
            result.extra["contact_name"] = contact
        result.extra["detail_url"] = url

        self.log.info(
            "connector.detail_fetched",
            reference=tender.reference,
            fields=len(result.extra),
            description_chars=len(result.description or ""),
        )
        return None if result.is_empty() else result

    @staticmethod
    def _read_labelled_values(html: str, labels: dict[str, str]) -> dict[str, str]:
        """Pull "Label → value" pairs out of the rendered page.

        The detail view is a flat run of label/value pairs rather than a table
        with addressable cells, so the label itself is the anchor. Reading text
        rather than structure is what makes this survive a re-layout — and the
        labels are user-visible, so a change to them is a change the portal's
        own users would notice too.
        """
        from bs4 import BeautifulSoup

        text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        joined = "\n".join(lines)

        found: dict[str, str] = {}
        for field_name, label in labels.items():
            # Value follows the label, either on the same line or the next one.
            pattern = re.compile(
                rf"{re.escape(str(label))}\s*[:\n]?\s*(.+)", re.IGNORECASE
            )
            match = pattern.search(joined)
            if not match:
                continue
            value = match.group(1).split("\n")[0].strip()
            # A label immediately followed by another label means the field is
            # simply empty; recording the next label as its value would be worse
            # than recording nothing.
            if value and not any(
                value.lower().startswith(str(other).lower())
                for other in labels.values()
                if other != label
            ):
                found[field_name] = value[:8000]
        return found
