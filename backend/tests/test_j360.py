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

import pytest

from app.connectors.config import load_connector_config
from app.connectors.j360.connector import J360Connector
from app.connectors.models import FetchedPage

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
        assert records[0].source_url.startswith("https://app.j360.info/#/announce/")

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
    def test_it_targets_the_real_endpoint(self):
        config = load_connector_config("j360")
        assert config.base_url == "https://app.j360.info"
        assert config.endpoints["search"] == "/api/searches/{search_id}/announces"

    def test_it_follows_the_drf_next_link(self):
        assert load_connector_config("j360").pagination["mode"] == "next_url"

    def test_detail_downloads_are_disabled_because_they_are_metered(self):
        """Opening an announcement is a paid action (~€9). An overnight
        schedule that fetched every detail could generate a large bill."""
        assert load_connector_config("j360").documents_policy["download"] is False

    def test_it_is_single_threaded_and_slow(self):
        config = load_connector_config("j360")
        assert config.http_get("concurrency.per_connector") == 1
        assert config.http_get("rate_limit.requests_per_second") <= 0.5
        assert config.pagination["max_pages"] <= 5

    def test_token_refresh_is_configured(self):
        refresh = load_connector_config("j360").auth["token_refresh"]
        assert refresh["enabled"] is True
        assert refresh["refresh_cookie"] == "JWT-refresh"
        assert refresh["access_cookie"] == "JWT-access"

    def test_at_least_one_saved_search_is_defined(self):
        searches = load_connector_config("j360").get("saved_searches")
        assert searches and searches[0]["id"] == 57549


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
