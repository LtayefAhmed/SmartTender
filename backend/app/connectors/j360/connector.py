"""J360 connector — DRF/JWT API behind a browser-captured session.

Access model, from the real contract:

    GET /api/searches/{id}/announces?order_by=date_limite
    → {count, next, previous, results[], current_page, total_pages, paginate_by}

The login sits behind an anti-bot layer, so it is captured once in a browser;
the crawl then runs on httpx. J360 issues a ~15-minute access token alongside a
multi-year refresh token, so the base class renews the access token itself
rather than declaring the session dead every quarter of an hour.

Three things here are genuinely J360-specific:

**Saved searches are the unit of work.** The account curates its criteria in
J360's UI — countries, activities, keyword includes and excludes. Crawling a
saved search inherits all of that, and is far more robust than reconstructing
the filter state through query parameters that can be renamed.

**Results carry no description or URL**, only search-highlight fragments. Both
have to be synthesised, and the fragments are the only free text available —
the full record is behind a metered detail view.

**Provenance matters more than usual.** J360 re-publishes portals we already
scrape directly (TUNEPS among them), so recording which upstream source each
notice came from is what explains why deduplication collapses so many of them.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from app.connectors.generic.api_connector import JsonApiConnector
from app.connectors.models import FetchedPage, NormalizedTender, RawRecord
from app.connectors.registry import register
from app.core.exceptions import AuthenticationError, CredentialsMissingError, ParsingError
from app.schemas.filters import TenderFilters

__all__ = ["J360Connector"]

#: `highlighted` fragments arrive as HTML with <em> around the matched terms.
_EM = re.compile(r"</?em>", re.IGNORECASE)


@register("j360")
class J360Connector(JsonApiConnector):
    """Paid multi-country tender aggregator."""

    async def authenticate(self) -> None:
        try:
            await super().authenticate()
        except (AuthenticationError, CredentialsMissingError) as exc:
            # A missing or lapsed session is the common failure and has a
            # specific fix. Naming it beats a generic auth error that leaves an
            # operator guessing between an expired subscription, a wrong
            # password, and a stale cookie jar.
            raise type(exc)(
                "J360 has no usable session. Sign in once with:\n"
                "    smarttender-admin capture-login j360\n"
                "If the refresh token has expired (it lasts years), the same "
                "command renews it.",
                connector=self.key,
                context={**exc.context, "auth_mode": "browser_session"},
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    async def fetch(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        """Crawl each configured saved search in turn.

        The saved search *is* the query, so filters are recorded for the run
        report and then applied client-side by the base class.
        """
        self.build_query(filters)

        searches = self.config.get("saved_searches") or []
        if not searches:
            raise ParsingError(
                "No saved searches are configured. Add at least one under "
                "`saved_searches:` in config/connectors/j360.yaml — the id is "
                "the number in app.j360.info/#/my-monitoring/searches/<id>.",
                connector=self.key,
            )

        template = self.endpoint("search")
        base_query = dict(self.config.get("default_query") or {})

        for search in searches:
            if self.out_of_time:
                self.log.warning("connector.deadline_reached_between_searches")
                return

            search_id = search.get("id") if isinstance(search, dict) else search
            label = search.get("name") if isinstance(search, dict) else str(search_id)
            if not search_id:
                continue

            path = template.format(search_id=search_id)
            url = f"{self.config.base_url.rstrip('/')}{path}"
            if base_query:
                url = f"{url}?{urlencode(base_query)}"

            self.log.info("connector.saved_search", search_id=search_id, name=label)
            self._current_search = {"id": str(search_id), "name": label}

            async for page in self._paginate(url):
                yield page

    async def _paginate(self, url: str) -> AsyncIterator[FetchedPage]:
        """Walk one saved search by following the API's own `next` link."""
        assert self.http is not None
        pagination = self.config.pagination
        next_path = pagination.get("next_response_path") or "next"
        from app.connectors.parsing.selectors import extract_json_path

        current: str | None = url
        for _ in range(self.max_pages):
            if self.out_of_time or not current:
                return

            await self._ensure_authenticated()
            page = await self.http.get(current, check_robots=False)
            self.note_page()
            yield page

            try:
                body = page.json()
            except Exception as exc:
                raise ParsingError(
                    "J360 returned a body that is not valid JSON.",
                    connector=self.key,
                    url=page.url,
                    cause=exc,
                ) from exc

            if not (extract_json_path(body, "results") or []):
                return
            current = extract_json_path(body, next_path)

    # ------------------------------------------------------------------
    #: J360-specific fields kept beyond the canonical mapping. The base parser
    #: only retains what `response_mapping.item` names — correctly, since that
    #: is the canonical contract — so anything used solely to enrich `extra`
    #: has to be carried over deliberately.
    _EXTRA_FIELDS = (
        "highlighted",
        "paid",
        "viewed",
        "lots_count",
        "execution_places_display",
        "is_recommendation",
        "special_criterion",
    )

    def parse(self, page: FetchedPage) -> list[RawRecord]:
        records = super().parse(page)
        search = getattr(self, "_current_search", {})

        # Re-read the payload to recover the fields the canonical mapping drops.
        # Positional zip is safe: both walk the same `results` array in order.
        from app.connectors.parsing.selectors import extract_json_path

        try:
            raw_items = extract_json_path(page.json(), "results") or []
        except Exception:
            raw_items = []

        for index, record in enumerate(records):
            identifier = record.get("external_id")
            # The API returns no URL. Build the app's deep link so an operator
            # can open the notice — and so each record has the distinct
            # `source_url` that ingestion and dedup key on.
            if identifier:
                record.fields["source_url"] = (
                    f"{self.config.base_url.rstrip('/')}/#/announce/{identifier}"
                )
                record.source_url = record.fields["source_url"]

            if index < len(raw_items) and isinstance(raw_items[index], dict):
                for field in self._EXTRA_FIELDS:
                    if field in raw_items[index]:
                        record.fields[field] = raw_items[index][field]

            if search:
                record.fields["saved_search_id"] = search.get("id")
                record.fields["saved_search_name"] = search.get("name")
        return records

    # ------------------------------------------------------------------
    def normalize(self, record: RawRecord) -> NormalizedTender:
        tender = super().normalize(record)
        raw = record.fields

        # `highlighted` is the only free text the list endpoint exposes — the
        # full description sits behind a metered detail view. The fragments are
        # search context rather than prose, but they carry the technical terms
        # that scoring keys on, which is exactly what makes them worth keeping.
        if not tender.description:
            fragments = raw.get("highlighted") or []
            if isinstance(fragments, list) and fragments:
                cleaned = [_EM.sub("", str(f)).strip() for f in fragments]
                tender.description = " … ".join(c for c in cleaned if c)[:4000]
                tender.extra["description_source"] = "search_highlights"

        # Aggregator provenance. J360 re-publishes portals we already scrape
        # directly, so this is what explains a high duplicate rate rather than
        # it looking like a bug.
        for field, key in (("source", "upstream_source"), ("source_website", "upstream_domain")):
            value = raw.get(field)
            if value:
                tender.extra[key] = value

        if raw.get("external_id"):
            tender.extra["j360_id"] = raw["external_id"]
        for field in ("saved_search_id", "saved_search_name"):
            if raw.get(field):
                tender.extra[field] = raw[field]

        # Billing visibility: `paid` marks whether the full record has been
        # unlocked on this account. Surfacing it stops anyone assuming the
        # description is complete when it is only a highlight fragment.
        if raw.get("paid") is not None:
            tender.extra["j360_paid"] = bool(raw["paid"])
        if raw.get("lots_count"):
            tender.extra["lots_count"] = raw["lots_count"]

        # Multi-country notices (UN and similar) list every eligible country;
        # that is a catalogue, not a place of performance, so it is not treated
        # as the tender's country.
        places = raw.get("execution_places_display")
        if isinstance(places, dict) and places.get("size", 0) > 5:
            tender.extra["execution_places_count"] = places["size"]
            tender.location = None

        return tender
