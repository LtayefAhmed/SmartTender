"""J360 connector, pinned against the real captured API response.

The payload below is a genuine capture from
``GET /api/searches/57549/announces?order_by=date_limite`` — abbreviated only in
the free-text fields. Testing against the real shape is what makes these
regressions meaningful: if J360 renames a field, these fail before a scheduled
run silently records nothing.
"""

from __future__ import annotations

import base64
import json
import time
from decimal import Decimal

import pytest

from app.connectors.config import load_connector_config
from app.connectors.j360.connector import J360Connector
from app.connectors.models import FetchedPage
from app.schemas.filters import TenderFilters

# --- real response, trimmed ---------------------------------------------------
J360_RESPONSE = {
    "count": 3,
    "next": None,
    "previous": None,
    "current_page": 1,
    "next_page_number": None,
    "previous_page_number": None,
    "paginate_by": 20,
    "total_pages": 1,
    "results": [
        {
            "id": 55822711,
            "title": (
                "APPEL D’OFFRES N°03/2026 portant sur « l’Acquisition et renouvellement "
                "de licences des plateformes et des équipements mis en exploitation au "
                "niveau de l’ANCE »"
            ),
            "buyer": "Agence Nationale de Certification Electronique",
            "buyer_place": {
                "country_code": "TN",
                "country_name": "Tunisie",
                "lat": 33.886917,
                "lon": 9.537499,
            },
            "announce_category": "Marché en cours",
            "announce_type": "MC",
            "date_publication": "2026-07-06T07:39:22",
            "limit_date": "2026-08-10T10:00:00",
            "amount": None,
            "lots_count": 12,
            "source_name": "TN - TUNEPS",
            "source_domain": "www.tuneps.tn",
            "execution_places_display": None,
            "highlighted": [
                "tisation CI/CD - Acquisition de licences d’un <em>système</em> de gestion",
                "ion de licences d’un système de gestion des <em>informations</em> et des "
                "événements",
            ],
            "viewed": True,
            "paid": False,
            "is_recommendation": False,
        },
        {
            "id": 55822811,
            "title": (
                "Acquisition et renouvellement de licence des équipements et solutions "
                "mis en exploitation au niveau de l'ANCE"
            ),
            "buyer": "Agence Nationale de Certification Electronique",
            "buyer_place": {"country_code": "TN", "country_name": "Tunisie"},
            "announce_category": "Marché en cours",
            "announce_type": "MC",
            "date_publication": "2026-07-06T07:56:37",
            "limit_date": "2026-08-10T10:00:00",
            "amount": None,
            "source_name": "TN - Plateforme des marchés publics",
            "source_domain": "www.marchespublics.gov.tn",
            "highlighted": ["Objet Acquisition de licences d’un <em>système</em> de gestion"],
            "viewed": True,
            "paid": False,
        },
        {
            "id": 55974864,
            "title": "Provision of Software and Application Development Services",
            "buyer": "United Nations",
            "buyer_place": {"country_code": "US", "country_name": "États-Unis"},
            "announce_category": "Marché en cours",
            "announce_type": "MC",
            "date_publication": "2026-07-15T00:00:00",
            "limit_date": "2026-08-30T00:00:00",
            "amount": None,
            "source_name": "OI - ONU - United Nations Procurement Division",
            "source_domain": "www.un.org",
            "special_criterion": ["IE_OI_ONG", "INDIV"],
            # The UN lists every eligible country — a catalogue, not a place.
            "execution_places_display": {
                "type": "countries",
                "size": 212,
                "display": "Irlande, Palaos, ... Finlande",
            },
            "highlighted": [
                "port and security for platforms such as Oracle, <em>SAP</em>, ServiceNow",
            ],
            "viewed": True,
            "paid": False,
        },
    ],
}


def _page() -> FetchedPage:
    return FetchedPage(
        url="https://app.j360.info/api/searches/57549/announces?order_by=date_limite",
        status_code=200,
        content=json.dumps(J360_RESPONSE).encode("utf-8"),
        headers={"content-type": "application/json"},
        encoding="utf-8",
    )


@pytest.fixture()
def connector() -> J360Connector:
    c = J360Connector(load_connector_config("j360"))
    c._current_search = {"id": "57549", "name": "MBO_TUN"}
    return c


# ---------------------------------------------------------------------------
class TestParsing:
    def test_it_reads_every_result(self, connector):
        assert len(connector.parse(_page())) == 3

    def test_each_record_gets_a_distinct_deep_link(self, connector):
        """The API returns no URL, so one is synthesised — and it must be
        unique per record or in-run dedup collapses the whole page."""
        records = connector.parse(_page())
        urls = {r.source_url for r in records}

        assert len(urls) == 3
        assert "55822711" in records[0].source_url
        assert records[0].source_url.startswith(
            "https://app.j360.info/#/my-monitoring/announce/"
        )

    def test_three_announcements_survive_in_run_deduplication(self, connector):
        """The in-run dedup key is the *canonical* URL, and J360 addresses
        announcements with a hash route. When canonicalisation dropped the
        fragment as noise, all three collapsed to `https://app.j360.info/` and
        two real tenders were silently discarded from a run that reported
        success. This is the assertion that catches that."""
        from app.core.identity import canonicalize_url

        records = connector.parse(_page())
        canonical = {canonicalize_url(r.source_url) for r in records}

        assert len(canonical) == 3

    def test_the_saved_search_is_recorded_on_each_record(self, connector):
        record = connector.parse(_page())[0]
        assert record.fields["saved_search_id"] == "57549"
        assert record.fields["saved_search_name"] == "MBO_TUN"


class TestNormalisation:
    def _tenders(self, connector):
        return [connector.normalize(r) for r in connector.parse(_page())]

    def test_core_fields_map_from_the_real_payload(self, connector):
        tender = self._tenders(connector)[0]

        assert "ANCE" in tender.title
        assert tender.buyer == "Agence Nationale de Certification Electronique"
        assert tender.country == "Tunisie"
        assert tender.external_id == "55822711"
        assert tender.deadline is not None and tender.deadline.year == 2026
        assert tender.publication_date is not None

    def test_highlight_fragments_become_the_description(self, connector):
        """The list endpoint exposes no description — the full record is behind
        a metered detail view — so the search highlights are the only free text
        available, and they carry the technical terms scoring keys on."""
        tender = self._tenders(connector)[0]

        assert tender.description
        assert "<em>" not in tender.description        # markup stripped
        assert "système" in tender.description
        assert tender.extra["description_source"] == "search_highlights"

    def test_upstream_provenance_is_preserved(self, connector):
        """J360 re-publishes portals we already scrape. Recording the upstream
        source is what explains a high duplicate rate instead of it looking
        like a bug."""
        tenders = self._tenders(connector)

        assert tenders[0].extra["upstream_source"] == "TN - TUNEPS"
        assert tenders[0].extra["upstream_domain"] == "www.tuneps.tn"
        assert "marchespublics" in tenders[1].extra["upstream_domain"]

    def test_the_same_tender_from_two_portals_looks_like_a_duplicate(self, connector):
        """Results 1 and 2 are the same ANCE licence renewal, published on
        TUNEPS and on marchespublics. Semantic dedup must see them as one."""
        from app.services.deduplication import DeduplicationService

        dedup = DeduplicationService()
        tenders = self._tenders(connector)
        from app.services.similarity import get_similarity_backend

        similarity = get_similarity_backend().similarity(
            dedup.comparison_key(tenders[0]), dedup.comparison_key(tenders[1])
        )
        # Same buyer, same deadline, same subject in different words.
        assert similarity > 0.5

    def test_billing_state_is_surfaced(self, connector):
        """`paid: false` means the full record is still locked. Surfacing it
        stops anyone treating a highlight fragment as a complete description."""
        assert self._tenders(connector)[0].extra["j360_paid"] is False

    def test_a_global_notice_does_not_claim_212_locations(self, connector):
        """The UN lists every eligible country; that is a catalogue, not a
        place of performance."""
        un = self._tenders(connector)[2]

        assert un.location is None
        assert un.extra["execution_places_count"] == 212
        assert un.country == "États-Unis"

    def test_lot_count_is_kept(self, connector):
        assert self._tenders(connector)[0].extra["lots_count"] == 12


class TestConfiguration:
    def test_it_targets_the_parametrised_search_endpoint(self):
        """The saved-search endpoint returns a frozen set curated in J360's UI,
        so our filters could only ever narrow it. `/api/announces` takes the
        criteria as parameters, which is what makes a keyword search reach the
        whole catalogue instead of a three-item slice."""
        config = load_connector_config("j360")

        assert config.base_url == "https://app.j360.info"
        assert config.endpoints["search"] == "/api/announces"
        assert config.get("search_mode") == "query"
        assert config.endpoints["saved_search"] == "/api/searches/{search_id}/announces"

    def test_only_live_tenders_are_requested_by_default(self):
        """J360's own UI defaults to every announce type, including archives and
        award results — which is why a browser search surfaces notices closed
        years ago. A detection tool wants what can still be bid on."""
        assert load_connector_config("j360").get("default_query")["type"] == "mc"

    def test_it_follows_the_drf_next_link(self):
        assert load_connector_config("j360").pagination["mode"] == "next_url"

    def test_detail_downloads_are_enabled_after_measuring_the_cost(self):
        """This was disabled on an assumption that measurement disproved.

        /api/me exposes a seat-based quota (additional_user_price per extra
        USER, has_reached_users_limit) and no announcement counter. Fetching
        the detail of an unviewed announcement returned 200 with full content
        and left every counter identical. The detail carries the budget, the
        full description and the attached files — the material the scorer and
        the CV-matching module actually need."""
        assert load_connector_config("j360").documents_policy["download"] is True

    def test_it_is_single_threaded_and_slow(self):
        """The session identifies the account on every request. Volume is what
        gets noticed, so concurrency stays at one and the rate stays low."""
        config = load_connector_config("j360")
        assert config.http_get("concurrency.per_connector") == 1
        assert config.http_get("rate_limit.requests_per_second") <= 0.5
        # Now that filtering happens server-side, pages carry matches rather
        # than candidates — but the cap stays modest on a metered account.
        assert config.pagination["max_pages"] <= 10

    def test_token_refresh_is_configured(self):
        refresh = load_connector_config("j360").auth["token_refresh"]
        assert refresh["enabled"] is True
        assert refresh["refresh_cookie"] == "JWT-refresh"
        assert refresh["access_cookie"] == "JWT-access"

    def test_at_least_one_saved_search_is_defined(self):
        searches = load_connector_config("j360").get("saved_searches")
        assert searches and searches[0]["id"] == 57549


class TestServerSideFiltering:
    """Pinned against a real captured request:

        GET /api/announces?countries=MR&countries=TN&op=AND&order_by=-created
            &q_simple=[{"value":"cloud","exact":false}]&search_all_fields=true
            &trades=60&trades=133&trades=132&trades=29&trades=105&type=mc,ma,rm,ab,ap
        → count: 109, total_pages: 6

    That request is why this mode exists: the same account's saved search
    returned three notices, so a keyword search against it could only ever
    return a subset of three. Pushing the criteria to the portal is what makes
    "give me forty results" reach forty real tenders.
    """

    def _query(self, **kwargs):
        connector = J360Connector(load_connector_config("j360"))
        return connector, connector._build_j360_query(TenderFilters(**kwargs))

    def test_keywords_use_the_portals_json_term_format(self):
        _, query = self._query(keywords=["cloud"])
        assert json.loads(query["q_simple"]) == [{"value": "cloud", "exact": False}]

    def test_several_keywords_are_all_sent(self):
        _, query = self._query(keywords=["cloud", "erp"])
        assert [t["value"] for t in json.loads(query["q_simple"])] == ["cloud", "erp"]

    def test_country_names_become_iso_codes(self):
        """Our vocabulary is country names; J360's is ISO alpha-2."""
        _, query = self._query(countries=["Tunisie", "Mauritanie"])
        assert sorted(query["countries"]) == ["MR", "TN"]

    def test_an_unmappable_country_falls_back_to_local_filtering(self):
        """Dropping it would silently widen the search — the user asked for
        Japan and would receive the world without being told."""
        connector, query = self._query(countries=["Japon"])

        assert "countries" not in query or "JP" not in (query.get("countries") or [])
        assert "countries" in connector._filter_application.client_side

    def test_sectors_become_trade_ids(self):
        _, query = self._query(sectors=["Consulting IT", "Développement informatique"])
        assert sorted(query["trades"]) == [132, 133]

    def test_the_run_report_records_what_the_portal_honoured(self):
        """Which criteria the portal applied decides whether a deep crawl is
        efficient or merely thorough, so it is reported rather than assumed."""
        connector, _ = self._query(
            keywords=["cloud"], countries=["Tunisie"], excluded_keywords=["formation"]
        )
        application = connector._filter_application

        assert "keywords" in application.server_side
        assert "countries" in application.server_side
        assert "excluded_keywords" in application.client_side

    def test_dates_are_pushed_down(self):
        from datetime import date

        _, query = self._query(deadline_from=date(2026, 8, 1))
        assert query["date_limite_after"] == "2026-08-01"

    def test_defaults_match_what_the_portals_own_ui_sends(self):
        _, query = self._query(keywords=["cloud"])

        assert query["order_by"] == "-created"     # append-only: no drift
        assert query["search_all_fields"] is True  # body text, not titles only
        assert query["op"] == "AND"


class TestJwtLifetimes:
    """The 15-minute access token is why automatic refresh exists at all."""

    @staticmethod
    def _jwt(token_type: str, lifetime_seconds: int) -> str:
        now = int(time.time())
        payload = {
            "token_type": token_type,
            "iat": now,
            "exp": now + lifetime_seconds,
            "user_id": "332491",
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
        return f"header.{encoded.decode()}.signature"

    def _session(self, access_lifetime: int, refresh_lifetime: int = 126_144_000):
        from app.connectors.http.session_store import BrowserSession

        return BrowserSession(
            cookies={
                "JWT-access": self._jwt("access", access_lifetime),
                "JWT-refresh": self._jwt("refresh", refresh_lifetime),
                "sessionid": "opaque-value",
            },
            origins=["https://app.j360.info"],
        )

    def test_it_reads_the_expiry_from_the_token(self):
        session = self._session(access_lifetime=900)
        expiry = session.token_expiry("JWT-access")
        assert expiry is not None

    def test_a_token_about_to_lapse_is_not_fresh(self):
        """A 30-second-old token would expire mid-request; refresh first."""
        session = self._session(access_lifetime=30)
        assert session.token_is_fresh("JWT-access", margin_seconds=120) is False
        assert session.token_is_fresh("JWT-refresh", margin_seconds=120) is True

    def test_a_healthy_token_is_fresh(self):
        session = self._session(access_lifetime=900)
        assert session.token_is_fresh("JWT-access", margin_seconds=120) is True

    def test_an_opaque_cookie_is_treated_as_fresh(self):
        """A non-JWT session has no readable expiry — it is the server's job to
        reject it, not ours to guess."""
        assert self._session(900).token_is_fresh("sessionid") is True

    def test_the_summary_reports_expiry_without_leaking_the_token(self):
        described = self._session(access_lifetime=900).describe()

        assert described["access_expires_in_min"] == pytest.approx(15, abs=1)
        assert described["refresh_expires_in_min"] > 1_000_000
        assert "eyJ" not in str(described)      # no token material


class TestRefreshEndpointDiscovery:
    """Which path renews the token is a deployment detail we cannot read off a
    session file. Probing a short candidate list means a crawl runs without an
    operator first capturing it from DevTools."""

    def _connector(self) -> J360Connector:
        return J360Connector(load_connector_config("j360"))

    def test_configured_paths_come_first_and_fallbacks_follow(self):
        connector = self._connector()
        config = load_connector_config("j360").auth["token_refresh"]

        candidates = connector._refresh_endpoints(config)

        assert candidates[0] == "/api/token/refresh/"
        assert "/api/token/renew/" in candidates
        assert len(candidates) == len(set(candidates))     # configured != duplicated

    def test_a_proven_path_is_pinned(self):
        """Once one works, the rest of the crawl must not replay the 404s —
        J360 counts every request against a session it can identify."""
        connector = self._connector()
        connector._refresh_endpoint = "/api/token/renew/"

        assert connector._refresh_endpoints({"endpoint": "/api/token/refresh/"}) == [
            "/api/token/renew/"
        ]

    def test_a_bare_string_endpoint_still_works(self):
        """Older configs wrote a single path; they must not break."""
        connector = self._connector()
        candidates = connector._refresh_endpoints({"endpoint": "/custom/renew/"})

        assert candidates[0] == "/custom/renew/"


class TestTheDeepLinkOpensTheRealAnnouncement:
    """`/announce/:announceId` is a *popup* state in J360's AngularJS router — a
    modal meant to open over a parent page, not a route of its own. Reached
    directly it silently falls back to the search screen, so the link looked
    broken while returning HTTP 200. The addressable form is the parent state's
    path plus the popup segment.

    Read off the shipped bundle, not guessed:
        getPopupStateConfig = () => ({ url: '/announce/:announceId', ... })
        states: my-monitoring.announce, my-monitoring.folder.announce, ...
    """

    def test_the_url_is_nested_under_its_parent_state(self):
        connector = J360Connector(load_connector_config("j360"))
        record = connector.parse(_page())[0]

        assert record.source_url == (
            "https://app.j360.info/#/my-monitoring/announce/55822711"
        )

    def test_the_bare_popup_route_is_not_used(self):
        """Regression guard: `#/announce/<id>` redirects to search."""
        connector = J360Connector(load_connector_config("j360"))

        for record in connector.parse(_page()):
            assert "/#/announce/" not in record.source_url


class TestNonProcurementNoticesAreFlagged:
    """J360 aggregates more than tenders.

    A recruitment ad for an "Assistant administratif (H/F)" whose description
    mentions "une première expérience sur Sage X3" scores exactly like a real
    ERP tender — the scoring is not wrong, the notice simply is not a contract
    to bid on. J360's own `special_criterion` carries the distinction; keeping
    it is what lets anyone act on it.
    """

    def test_staffing_notices_are_marked(self, connector):
        un = [connector.normalize(r) for r in connector.parse(_page())][2]

        assert un.extra["is_staffing_offer"] is True
        assert "IE_OI_ONG" in un.extra["j360_criteria"]

    def test_an_ordinary_tender_is_not_marked(self, connector):
        ance = next(connector.normalize(r) for r in connector.parse(_page()))

        assert "is_staffing_offer" not in ance.extra


class TestAttachments:
    """J360 does publish downloadable notices, on some announcements.

    Measured on a live page: 3 of 20 carried an `attached_files` entry shaped
    {url, name}, pointing at /api/announces/{id}/attached_file?fid=... . That
    URL returns a real .docx (19 KB) with the session and 401 without it —
    which is why the downloader has to carry the connector's session rather
    than fetching anonymously.
    """

    def _page_with_files(self):
        payload = json.loads(json.dumps(J360_RESPONSE))
        payload["results"] = payload["results"][:1]
        return payload

    def test_an_attachment_becomes_a_document_reference(self, connector):

        detail_body = {
            "id": 56232383,
            "additional_information": "Avis d'appel d'offres travaux",
            "attached_files": [
                {
                    "url": "https://app.j360.info/api/announces/56232383/"
                    "attached_file?fid=14219240",
                    "name": "Avis d’appel d’offres",
                }
            ],
        }
        result = connector._detail_from_body(detail_body)

        assert len(result.documents) == 1
        assert result.documents[0].name == "Avis d’appel d’offres"
        assert "attached_file?fid=" in result.documents[0].url

    def test_an_announcement_without_attachments_yields_none(self, connector):
        result = connector._detail_from_body({"id": 1, "attached_files": []})
        assert result.documents == []

    def test_the_contract_total_is_preferred_over_a_lot_figure(self, connector):
        """A per-lot amount would understate the opportunity."""
        result = connector._detail_from_body(
            {
                "amounts": [
                    {"type": "lot", "amount": 50000.0, "currency": "€"},
                    {"type": "total", "amount": 2000000.0, "currency": "€"},
                ]
            }
        )

        assert result.estimated_budget == Decimal("2000000.0")
        assert result.currency == "EUR"


class TestThePublicationIsTheRichestSource:
    """"Voir le détail" is not in the JSON — and it is the whole point.

    Measured on live announcements:

        award result (CNI)      540 chars — Id, amounts ex/inc tax, awardee, RNE
        framework (KOATY)     9 390 chars — the full "Avis de marché", all sections
        UN software           12 471 chars — the complete Request for Proposal

    `additional_information` was empty on two of those three. The publication
    exists for every announcement, unlike attachments, which only ~15% carry —
    so this, not the attachments, is what gives a CV something to match against.
    """

    def test_the_publication_url_is_recognised(self, connector):
        body = {
            "id": 56189132,
            "external_url": (
                "https://j360-ext.info/announces/56189132/description/332491/token/"
            ),
        }
        result = connector._detail_from_body(body)

        # The mapping itself does not fetch; the URL is what fetch_detail follows.
        assert body["external_url"] in result.source_links

    def test_a_non_publication_url_is_not_followed(self, connector):
        """Only j360-ext links are the signed publication; the rest are the
        originating portals, which need their own credentials."""
        import asyncio

        assert asyncio.run(connector._fetch_publication("https://www.un.org/notice")) is None
        assert asyncio.run(connector._fetch_publication(None)) is None


class TestThePublicationIsNotTruncated:
    """A cap must sit above the real distribution, not through the middle of it.

    Measured: a first ceiling of 20 000 characters cut 8 of 23 stored
    publications at exactly that figure — a third of the corpus. The lost tail
    is where award criteria and required profiles sit, because standardised
    notices put administrative sections first and evaluation last. The cap was
    silently removing the only part that matters for CV matching.
    """

    def test_the_ceiling_is_far_above_the_observed_maximum(self):
        from app.connectors.j360.connector import _MAX_PUBLICATION_CHARS

        # Longest publication actually observed was ~30 000 characters once the
        # first cap was lifted; the ceiling exists for a runaway page, not for
        # ordinary notices.
        assert _MAX_PUBLICATION_CHARS >= 100_000
