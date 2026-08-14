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

import json
import re
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from app.connectors.generic.api_connector import JsonApiConnector
from app.connectors.models import (
    DetailResult,
    DocumentRef,
    FetchedPage,
    NormalizedTender,
    RawRecord,
    TenderDetailRequest,
)
from app.connectors.parsing.links import DocumentLink, harvest_document_links
from app.connectors.registry import register
from app.core.exceptions import AuthenticationError, CredentialsMissingError, ParsingError
from app.schemas.filters import FilterApplication, TenderFilters

__all__ = ["J360Connector"]

#: `highlighted` fragments arrive as HTML with <em> around the matched terms.
_EM = re.compile(r"</?em>", re.IGNORECASE)

#: Ceiling on a stored publication. Deliberately generous: a first cut at 20 000
#: truncated a third of the corpus — every notice long enough to carry award
#: criteria and required profiles, which are exactly the parts that appear last.
#: A cap is still needed (a runaway page must not fill the column), but it has
#: to sit far above the real distribution rather than through the middle of it.
_MAX_PUBLICATION_CHARS = 200_000


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
        if str(self.config.get("search_mode") or "query").lower() == "saved_search":
            async for page in self._fetch_saved_searches(filters):
                yield page
            return

        async for page in self._fetch_query(filters):
            yield page

    async def _fetch_query(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        """Search J360 with our filters pushed down to the portal.

        This is the mode that makes result counts mean what a user expects.
        J360 applies the criteria itself, so every page returned is a page of
        *matches* — asking for forty results reaches forty real tenders instead
        of re-filtering the same two pages of an arbitrary slice.
        """
        # OR across terms, against a portal that only ANDs: run one search per
        # term and let the base class union the results. Pushing nothing
        # instead would mean paging the portal's entire worldwide feed and
        # discarding it locally — measured at 200 notices read for 1 kept.
        # Each search here returns actual matches, and in-run deduplication
        # collapses a notice that satisfies several terms.
        if filters.keywords_any and len(filters.keywords) > 1:
            async for page in self._fetch_per_keyword(filters):
                yield page
            return

        query = self._build_j360_query(filters)

        # Every requested country is unknown to the portal, so no notice can
        # carry one and the local filter would reject all of them. Crawling to
        # prove that costs 200 fetches and two minutes; the run report already
        # names the unrecognised values.
        if filters.countries and not query.get("countries"):
            self.log.warning(
                "connector.search_cannot_match",
                reason="no_recognised_country",
                requested=filters.countries,
            )
            return

        url = f"{self.config.base_url.rstrip('/')}{self.endpoint('search')}"
        if query:
            # YAML booleans would serialise as "True"; the API — and every
            # other Django backend — expects "true".
            encoded = {
                k: ("true" if v is True else "false" if v is False else v)
                for k, v in query.items()
            }
            url = f"{url}?{urlencode(encoded, doseq=True)}"

        self.log.info(
            "connector.query_search",
            countries=query.get("countries"),
            keywords=bool(query.get("q_simple")),
            trades=query.get("trades"),
        )
        self._current_search = {"id": None, "name": "recherche filtrée"}
        async for page in self._paginate(url):
            yield page

    async def _fetch_per_keyword(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        """One search per term, because the portal cannot express alternatives.

        The base class stops as soon as enough results have accumulated, so a
        common term usually satisfies the quota before the rarer ones are ever
        requested — which keeps the cost close to a single search rather than
        multiplying it by the number of terms.
        """
        terms = list(filters.keywords)
        self.log.info("connector.or_search", terms=len(terms))

        for index, term in enumerate(terms, start=1):
            if self.out_of_time:
                self.log.warning("connector.deadline_reached_between_terms", done=index - 1)
                return

            single = filters.model_copy(update={"keywords": [term], "keywords_any": False})
            query = self._build_j360_query(single)
            # Report the union, not the last term's application: every criterion
            # was honoured by the portal, once per search.
            self._filter_application.server_side = sorted(
                set(self._filter_application.server_side) | {"keywords"}
            )
            url = f"{self.config.base_url.rstrip('/')}{self.endpoint('search')}"
            encoded = {
                k: ("true" if v is True else "false" if v is False else v)
                for k, v in query.items()
            }
            url = f"{url}?{urlencode(encoded, doseq=True)}"

            self._current_search = {"id": None, "name": f"terme « {term} »"}
            async for page in self._paginate(url):
                yield page

    def _build_j360_query(self, filters: TenderFilters) -> dict[str, Any]:
        """Translate canonical filters into J360's query parameters.

        Recorded in the run report through ``build_query`` so the UI can show
        which criteria the portal honoured and which fell back to local
        filtering — the difference decides whether a deep crawl is efficient or
        merely thorough.
        """
        application = FilterApplication()
        mapping = self.config.filter_mapping
        values = self.config.filter_values
        query: dict[str, Any] = dict(self.config.get("default_query") or {})

        # Keywords: J360 takes a JSON array of terms, each with an `exact` flag.
        # `exact: false` is substring-ish matching, which is what a user typing
        # "cloud" means — and it is what the portal's own UI sends.
        # J360 combines `q_simple` terms with AND — measured, and its `op`
        # parameter makes no difference to the count. So a list of alternatives
        # cannot be pushed: asking for the seven Inetum domains at once sent 31
        # terms and returned zero, because no notice contains all of them.
        #
        # Under OR semantics we therefore push nothing and filter locally,
        # exactly as the TUNEPS connector does with its single search field.
        # Under AND semantics the whole list is safe to send.
        if filters.keywords:
            if not mapping.get("keywords"):
                application.client_side.append("keywords")
            # One term is one term: OR and AND describe the same request, so
            # there is nothing to disambiguate and it is always safe to send.
            # Refusing it meant paging the portal's worldwide feed for a single
            # keyword — measured at 200 notices read and 200 discarded.
            elif filters.keywords_any and len(filters.keywords) > 1:
                application.client_side.append("keywords")
                self.log.info(
                    "connector.keywords_kept_local",
                    reason="portal_ands_terms",
                    count=len(filters.keywords),
                )
            else:
                query[mapping["keywords"]] = json.dumps(
                    [{"value": k, "exact": False} for k in filters.keywords],
                    ensure_ascii=False,
                )
                application.server_side.append("keywords")

        # Countries: repeated parameters, ISO alpha-2. A name we cannot map is
        # kept client-side rather than dropped — silently ignoring it would
        # widen the search behind the user's back.
        if filters.countries:
            # Matched on a folded key. A user typing "tunisie" or "TUNISIE"
            # means Tunisia; a case-sensitive table silently demoted the filter
            # to local matching, so the portal returned the world and we threw
            # most of it away.
            from app.core.identity import normalize_text

            lookup = {
                normalize_text(name): [code]
                for name, code in (values.get("countries") or {}).items()
            }
            # A zone resolves to every country it holds. "Afrique" is 55 states;
            # naming the continent is what a user means, and enumerating them is
            # what the portal needs.
            for zone, zone_codes in (values.get("country_zones") or {}).items():
                lookup.setdefault(normalize_text(zone), list(zone_codes))

            codes: list[str] = []
            unmapped: list[str] = []
            for name in filters.countries:
                resolved = lookup.get(normalize_text(name))
                if resolved:
                    codes.extend(c for c in resolved if c not in codes)
                else:
                    unmapped.append(name)
            if codes and mapping.get("countries"):
                query[mapping["countries"]] = codes
                application.server_side.append("countries")
            if unmapped:
                # The reference table holds every country J360 knows, so a name
                # it does not contain is a typo rather than a gap. Saying so
                # beats spending two minutes proving it: local filtering on a
                # country no notice carries reads the portal's whole feed and
                # keeps none of it.
                application.unsupported.extend(unmapped)
                self.log.warning(
                    "connector.unknown_countries",
                    names=unmapped,
                    hint="absent from J360's own country reference",
                )
            if not codes:
                application.client_side.append("countries")

        if filters.sectors and mapping.get("sectors"):
            lookup = values.get("sectors") or {}
            trades = [lookup[s] for s in filters.sectors if s in lookup]
            if trades:
                query[mapping["sectors"]] = trades
                application.server_side.append("sectors")
            if len(trades) != len(filters.sectors):
                application.client_side.append("sectors")
        elif filters.sectors:
            application.client_side.append("sectors")

        for canonical, value in (
            ("publication_date_from", filters.publication_date_from),
            ("deadline_from", filters.deadline_from),
            ("deadline_to", filters.deadline_to),
        ):
            if value is None:
                continue
            param = mapping.get(canonical)
            if param:
                query[param] = value.isoformat()
                application.server_side.append(canonical)
            else:
                application.client_side.append(canonical)

        # Everything else stays local. Naming it keeps the run report honest
        # about what the portal did versus what we did after the fact.
        for canonical, value in (
            ("excluded_keywords", filters.excluded_keywords),
            ("organizations", filters.organizations),
            ("budget_min", filters.budget_min),
            ("statuses", filters.statuses),
        ):
            if value:
                application.client_side.append(canonical)

        self._filter_application = application
        return query

    async def _fetch_saved_searches(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        """Crawl saved searches curated in J360's own UI.

        Their criteria are frozen on J360's side, so our filters can only
        narrow the result — never widen it. Kept for standing watches the team
        maintains there, but it is not what the Scrape screen should drive.
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

        template = self.endpoint("saved_search")
        # `type` and `search_all_fields` belong to the query endpoint; a saved
        # search carries its own criteria and rejects nothing, but sending them
        # would silently override what the team curated.
        base_query = {
            k: v
            for k, v in (self.config.get("default_query") or {}).items()
            if k in {"order_by"}
        }

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
                # `/announce/:announceId` is a *popup* state in J360's router —
                # a modal designed to open over a parent page, not a route of
                # its own. Reached directly it falls back to the search screen,
                # which looks like the link is broken. The addressable form is
                # the parent state's path plus the popup segment.
                record.fields["source_url"] = (
                    f"{self.config.base_url.rstrip('/')}/#/my-monitoring/announce/{identifier}"
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

        # J360 aggregates more than procurement notices. `special_criterion`
        # carries the flags its own search form exposes — INDIV marks an
        # individual-consultant or temporary-work posting, IE_OI_ONG an
        # international-organisation notice. Without it a recruitment ad for an
        # "Assistant administratif" that happens to mention Sage X3 scores like
        # a genuine ERP tender, and nothing on screen says why it is different.
        criteria = raw.get("special_criterion")
        if criteria:
            tender.extra["j360_criteria"] = list(criteria)
            tender.extra["is_staffing_offer"] = "INDIV" in criteria

        # Multi-country notices (UN and similar) list every eligible country;
        # that is a catalogue, not a place of performance, so it is not treated
        # as the tender's country.
        places = raw.get("execution_places_display")
        if isinstance(places, dict) and places.get("size", 0) > 5:
            tender.extra["execution_places_count"] = places["size"]
            tender.location = None

        return tender

    def matches_filters(self, tender: NormalizedTender, filters: TenderFilters) -> bool:
        """Drop notices that are not contracts, then apply the usual filters.

        J360 aggregates recruitment alongside procurement. Those postings are
        real, correctly parsed, and correctly scored — an ad mentioning Sage X3
        genuinely matches the ERP profile — they simply are not something to
        bid on. Excluding them here rather than at validation is deliberate:
        this is a filtering decision, so it is counted as one and stays visible
        in the run report instead of masquerading as a parse failure.
        """
        excluded = {str(c).upper() for c in (self.config.get("exclude_criteria") or [])}
        if excluded:
            criteria = {str(c).upper() for c in (tender.extra.get("j360_criteria") or [])}
            if criteria & excluded:
                self.log.debug(
                    "connector.notice_excluded",
                    reason="special_criterion",
                    matched=sorted(criteria & excluded),
                )
                return False
        return super().matches_filters(tender, filters)

    # ------------------------------------------------------------------
    # Detail enrichment
    # ------------------------------------------------------------------
    #: Measured, not assumed — see the BILLING section of j360.yaml. The
    #: account's quota is seat-based; fetching a detail changed no counter.
    supports_detail = True

    async def fetch_detail(self, tender: TenderDetailRequest) -> DetailResult | None:
        """Open one announcement through ``GET /api/announces/{id}``.

        This is where J360 earns its subscription. The list endpoint returns a
        title, a buyer and search-highlight fragments; the detail carries the
        budget, the full description, the CPV nomenclature, the lots, and the
        links back to the portal that originally published the notice — which
        is where the tender documents themselves live, since J360 aggregates
        rather than hosts.
        """
        if not tender.external_id:
            return None

        assert self.http is not None
        page = await self.http.get(
            f"{self.config.base_url.rstrip('/')}/api/announces/{tender.external_id}",
            check_robots=False,
        )
        try:
            body = page.json()
        except Exception as exc:
            raise ParsingError(
                "J360 returned a detail body that is not valid JSON.",
                connector=self.key,
                url=page.url,
                cause=exc,
            ) from exc
        if not isinstance(body, dict):
            return None

        result = self._detail_from_body(body)

        # The structured publication — "Voir le détail" in J360's UI — is not in
        # the JSON. It is served as HTML from a signed j360-ext.info URL, and it
        # is the single richest thing this connector can reach: the full object,
        # the lot descriptions, the amounts excluding and including tax, the
        # awardee and its registration number. `additional_information` is often
        # empty while this is not, so it is fetched for every announcement
        # rather than only when the JSON looks thin.
        publication, links = await self._fetch_publication(body.get("external_url"))
        if publication:
            result.extra["publication_text"] = publication
            if len(publication) > len(result.description or ""):
                result.description = publication

        # The publication body carries its own attachments — "Règlement de
        # consultation - 1,1 Mo" sits inside the prose, not in the JSON's
        # document list. Flattening the page to text destroyed them, so the
        # richest documents a notice offers were the ones never collected.
        harvested = 0
        known = {document.url for document in result.documents}
        for link in links:
            if link.url in known:
                continue
            known.add(link.url)
            harvested += 1
            result.documents.append(DocumentRef(url=link.url, name=link.label))

        self.log.info(
            "connector.detail_fetched",
            announce_id=tender.external_id,
            has_budget=result.estimated_budget is not None,
            description_chars=len(result.description or ""),
            documents=len(result.documents),
            documents_from_publication=harvested,
            source_links=len(result.source_links),
        )
        return None if result.is_empty() else result

    async def _fetch_publication(
        self, external_url: Any
    ) -> tuple[str | None, list[DocumentLink]]:
        """Read the signed publication page: its text, and the files it links to.

        Best-effort: the URL carries a time-limited signature, so a stale one
        simply yields nothing and the announcement keeps whatever the JSON gave
        it. Losing this must never cost the tender.
        """
        if not external_url or "j360-ext" not in str(external_url):
            return None, []
        assert self.http is not None
        try:
            page = await self.http.get(
                str(external_url),
                check_robots=False,
                # Asked for explicitly. The client treats "requested JSON, got
                # HTML" as an authentication bounce — a good heuristic for the
                # API, and a false positive here, where an HTML document is
                # exactly what we want.
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except Exception as exc:
            self.log.info("connector.publication_unavailable", error=str(exc)[:150])
            return None, []

        from bs4 import BeautifulSoup

        # Links are read from the markup before it is flattened, because
        # `get_text` keeps "Règlement de consultation - 1,1 Mo" and discards the
        # href that makes it retrievable.
        links = harvest_document_links(page.text, base_url=str(external_url))

        text = BeautifulSoup(page.text, "lxml").get_text("\n", strip=True)
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        # The page opens with the publisher's own name; it is not tender content.
        if lines and lines[0].lower() in {"octopusmind", "j360"}:
            lines = lines[1:]
        return "\n".join(lines)[:_MAX_PUBLICATION_CHARS] or None, links

    def _detail_from_body(self, body: dict[str, Any]) -> DetailResult:
        """Map a detail payload onto the canonical result.

        Separated from the fetch so the mapping can be asserted against a
        captured payload without a session, a network, or a metered request.
        """
        result = DetailResult()
        result.description = self._clean(body.get("additional_information"))

        # `amounts` is a list of typed figures; the "total" is the contract
        # value. A per-lot figure would understate the opportunity.
        amount, currency = self._pick_amount(body.get("amounts"))
        if amount is not None:
            result.estimated_budget = amount
            result.currency = currency

        result.cpv_codes = self._cpv_codes(body)

        # The originating portals. J360 hosts no files of its own, so a tender's
        # cahier des charges is only reachable through these.
        links = [str(u) for u in (body.get("alternate_source_links") or []) if u]
        for key in ("url", "external_url", "website_url"):
            value = body.get(key)
            if value and str(value) not in links:
                links.append(str(value))
        # `links.others` carries the DCE (Dossier de Consultation des
        # Entreprises) download pages — the actual tender pack. They sit behind
        # a login on the originating portal, so they cannot be fetched
        # automatically; recording them is what lets a bid manager reach the
        # documents in one click instead of searching the portal by hand.
        for group in ("original", "others"):
            for url in (body.get("links") or {}).get(group) or []:
                cleaned = str(url).replace("&amp;", "&")
                if cleaned not in links:
                    links.append(cleaned)
        result.source_links = links[:20]

        for attachment in body.get("attached_files") or []:
            if not isinstance(attachment, dict):
                continue
            url = attachment.get("url") or attachment.get("file")
            if url:
                result.documents.append(
                    DocumentRef(url=str(url), name=(attachment.get("name") or None))
                )

        for key, target in (("lots", "lots"), ("awardees", "awardees")):
            value = body.get(key)
            if value:
                result.extra[target] = value
        if body.get("project_owner_email"):
            result.contact_email = str(body["project_owner_email"])

        return result

    @staticmethod
    def _clean(value: Any) -> str | None:
        if not value:
            return None
        text = _EM.sub("", str(value)).strip()
        return text[:_MAX_PUBLICATION_CHARS] or None

    @staticmethod
    def _pick_amount(amounts: Any) -> tuple[Decimal | None, str | None]:
        """Take the contract total, ignoring per-lot and estimated variants."""
        if not isinstance(amounts, list):
            return None, None
        chosen = None
        for entry in amounts:
            if not isinstance(entry, dict) or entry.get("amount") in (None, 0):
                continue
            if str(entry.get("type") or "").lower() == "total":
                chosen = entry
                break
            chosen = chosen or entry
        if not chosen:
            return None, None
        try:
            value = Decimal(str(chosen["amount"]))
        except (InvalidOperation, KeyError, TypeError):
            return None, None
        # The API returns the symbol, not the ISO code.
        symbol = str(chosen.get("currency") or "").strip()
        symbols = {"€": "EUR", "$": "USD", "DT": "TND", "TND": "TND"}
        currency = symbols.get(symbol, symbol[:8] or None)
        return value, currency

    @staticmethod
    def _cpv_codes(body: dict[str, Any]) -> list[str]:
        """Pull CPV codes from wherever this payload happens to carry them.

        The shape varies by originating portal — a list of dicts, a list of
        strings, or a nested `cpvs` block — so each form is handled rather than
        assuming the one seen first is the only one.
        """
        # Measured: J360's detail payload does NOT carry CPV as structured
        # data — the nomenclature shown in its UI is part of the publication
        # text rendered from the originating portal. These keys are read
        # anyway because the shape differs per upstream source, and a notice
        # syndicated from TED does expose them.
        codes: list[str] = []
        for key in ("cpv", "cpvs", "cpv_codes", "nomenclatures"):
            raw = body.get(key)
            if not raw:
                continue
            entries = raw if isinstance(raw, list) else [raw]
            for entry in entries:
                code = entry.get("code") if isinstance(entry, dict) else entry
                if code and str(code).strip().isdigit():
                    codes.append(str(code).strip())
        return sorted(set(codes))
