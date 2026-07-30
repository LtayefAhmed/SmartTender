"""Connector framework: the isolation invariant, the registry, and the fixture run.

The central assertion of this module is that ``BaseConnector.run`` **never
raises**. Everything else in the platform's failure handling is built on that
guarantee, so it is tested against every category of failure a connector can
produce: transport errors, parse errors, per-item errors, an open circuit,
missing credentials, and an outright bug in connector code.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.connectors.base import BaseConnector, ConnectorContext
from app.connectors.config import ConnectorConfig, load_connector_config
from app.connectors.models import FetchedPage, NormalizedTender, RawRecord
from app.connectors.registry import get_registry
from app.core.enums import FetchStrategy, JobTrigger
from app.core.exceptions import (
    ParsingError,
    SelectorBrokenError,
    SourceUnavailableError,
)
from app.schemas.filters import TenderFilters


def _config(**overrides) -> ConnectorConfig:
    raw = {
        "key": "probe",
        "name": "Probe",
        "strategy": "static",
        "required_fields": ["title"],
        **overrides.pop("raw", {}),
    }
    return ConnectorConfig(
        key="probe",
        name="Probe",
        enabled=overrides.pop("enabled", True),
        strategy=FetchStrategy.STATIC,
        base_url="https://probe.example.tn",
        raw=raw,
        # Disabled by default so the suite never reaches for Redis; the one
        # test that needs a breaker constructs its own.
        http={"circuit_breaker": {"enabled": False}},
    )


class _Probe(BaseConnector):
    """Minimal connector whose behaviour each test controls."""

    pages: list[FetchedPage] = []
    fetch_error: Exception | None = None
    parse_error: Exception | None = None
    normalize_error: Exception | None = None
    records_per_page: int = 2

    async def setup(self) -> None:
        self.http = None
        self._parsed_pages = 0

    async def fetch(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        if self.fetch_error:
            raise self.fetch_error
        for page in self.pages:
            self.note_page()
            yield page

    def parse(self, page: FetchedPage) -> list[RawRecord]:
        if self.parse_error:
            raise self.parse_error
        # Each page yields distinct URLs, as a real paginated listing does.
        # Tests that need repeats across pages override this.
        offset = self._parsed_pages * self.records_per_page
        self._parsed_pages += 1
        return [
            RawRecord(
                connector_key=self.key,
                source_url=f"https://probe.example.tn/avis/{offset + index}",
                fields={"title": f"Marché de développement numéro {offset + index}"},
            )
            for index in range(self.records_per_page)
        ]

    def normalize(self, record: RawRecord) -> NormalizedTender:
        if self.normalize_error:
            raise self.normalize_error
        return NormalizedTender(
            connector_key=self.key,
            source_url=record.source_url,
            title=record.get("title"),
        )


def _page() -> FetchedPage:
    return FetchedPage(
        url="https://probe.example.tn/list", status_code=200, content=b"<html></html>"
    )


def _run(connector: BaseConnector, filters: TenderFilters | None = None):
    return asyncio.run(connector.run(filters or TenderFilters()))


class TestIsolationInvariant:
    """A failure in one connector must never propagate out of ``run``."""

    def test_a_successful_run_returns_its_tenders(self):
        probe = _Probe(_config())
        probe.pages = [_page(), _page()]
        outcome = _run(probe)

        assert outcome.succeeded is True
        assert outcome.items_found == 4
        assert outcome.pages_fetched == 2

    def test_a_transport_failure_is_returned_not_raised(self):
        probe = _Probe(_config())
        probe.fetch_error = SourceUnavailableError("portal is down", connector="probe")
        outcome = _run(probe)

        assert outcome.succeeded is False
        assert outcome.error_type == "SourceUnavailableError"
        assert outcome.items_found == 0

    def test_an_unexpected_bug_is_contained(self):
        probe = _Probe(_config())
        probe.fetch_error = ZeroDivisionError("connector bug")
        outcome = _run(probe)

        assert outcome.succeeded is False
        assert outcome.error_type == "ZeroDivisionError"

    def test_a_broken_selector_aborts_the_run_and_alerts(self):
        # Continuing after the markup has moved would just record silent zeros.
        probe = _Probe(_config())
        probe.pages = [_page()]
        probe.parse_error = SelectorBrokenError("markup changed", selector="table.x")
        outcome = _run(probe)

        assert outcome.succeeded is False
        assert outcome.error_type == "SelectorBrokenError"

    def test_a_non_alerting_parse_error_skips_only_that_page(self):
        probe = _Probe(_config())
        probe.pages = [_page(), _page()]
        probe.parse_error = ParsingError("one bad page", connector="probe")
        probe.parse_error.alerting = False
        outcome = _run(probe)

        assert outcome.succeeded is True
        assert len(outcome.item_failures) == 2   # one per page, run completed

    def test_one_bad_item_does_not_lose_the_others(self):
        class PartlyBroken(_Probe):
            def normalize(self, record):
                if record.source_url.endswith("/1"):
                    raise ValueError("this record is malformed")
                return super().normalize(record)

        probe = PartlyBroken(_config())
        probe.pages = [_page()]
        probe.records_per_page = 4
        outcome = _run(probe)

        assert outcome.succeeded is True
        assert outcome.items_found == 3
        assert len(outcome.item_failures) == 1

    def test_recorded_item_failures_are_bounded(self):
        """A catastrophically broken portal must not write a multi-megabyte blob."""
        from app.connectors.base import MAX_RECORDED_ITEM_FAILURES

        probe = _Probe(_config())
        probe.pages = [_page()]
        probe.records_per_page = MAX_RECORDED_ITEM_FAILURES + 25
        probe.normalize_error = ValueError("always broken")
        outcome = _run(probe)

        assert len(outcome.item_failures) == MAX_RECORDED_ITEM_FAILURES


class TestSkipping:
    def test_a_disabled_connector_is_skipped_not_failed(self):
        outcome = _run(_Probe(_config(enabled=False)))
        assert outcome.skipped is True
        assert outcome.succeeded is True     # skipped is not failed
        assert outcome.skip_reason == "disabled"

    def test_missing_credentials_skip_quietly(self):
        config = _config(
            raw={"auth": {"credentials_env": {"api_key": "DEFINITELY_NOT_SET_12345"}}}
        )
        outcome = _run(_Probe(config))
        assert outcome.skipped is True
        assert outcome.skip_reason == "credentials_missing"

    def test_an_open_circuit_skips_without_touching_the_network(self):
        """An open circuit turns the run into a skip, not a failed fetch.

        Breaker state is shared between workers through Redis; here it is
        driven directly so the test needs no infrastructure.
        """
        from app.connectors.http.circuit_breaker import CircuitBreaker

        config = _config()
        config.http = {
            "circuit_breaker": {
                "enabled": True,
                "failure_threshold": 1,
                "recovery_timeout_seconds": 600,
            }
        }
        probe = _Probe(config)
        probe.breaker = CircuitBreaker("probe", config.http["circuit_breaker"])
        probe.breaker._redis_failed = True    # local state only
        asyncio.run(probe.breaker.record_failure())

        # fetch would raise if it were ever reached — it must not be.
        probe.fetch_error = SourceUnavailableError("down", connector="probe")
        outcome = _run(probe)

        assert outcome.skipped is True
        assert outcome.succeeded is True      # skipped is not failed
        assert outcome.skip_reason == "circuit_open"


class TestValidation:
    def test_required_fields_are_enforced(self):
        class NoTitle(_Probe):
            def parse(self, page):
                return [
                    RawRecord(connector_key=self.key, source_url="https://x.tn/1", fields={})
                ]

        probe = NoTitle(_config())
        probe.pages = [_page()]
        outcome = _run(probe)

        assert outcome.items_found == 0
        assert outcome.item_failures[0].error_type == "ValidationError"


class TestLimits:
    def test_the_item_limit_is_respected(self):
        probe = _Probe(_config(), ConnectorContext(max_items=3))
        probe.pages = [_page(), _page(), _page()]
        assert _run(probe).items_found == 3

    def test_in_run_duplicates_are_dropped(self):
        class Repeating(_Probe):
            def parse(self, page):
                return [
                    RawRecord(
                        connector_key=self.key,
                        source_url="https://probe.example.tn/avis/same",
                        fields={"title": "Le même avis répété sur chaque page"},
                    )
                ]

        probe = Repeating(_config())
        probe.pages = [_page(), _page(), _page()]
        # Paying for the downstream pipeline three times would be pure waste.
        assert _run(probe).items_found == 1

    def test_the_deadline_stops_the_run_cleanly(self):
        probe = _Probe(_config(), ConnectorContext(deadline_seconds=0.0))
        probe.pages = [_page(), _page()]
        outcome = _run(probe)
        assert outcome.succeeded is True   # a clean stop, not a failure


class TestClientSideFiltering:
    def test_keywords_the_portal_could_not_express_are_applied_locally(self):
        probe = _Probe(_config())
        probe.pages = [_page()]
        probe.records_per_page = 2

        assert _run(probe, TenderFilters(keywords=["développement"])).items_found == 2
        assert _run(probe, TenderFilters(keywords=["carburant"])).items_found == 0

    def test_excluded_keywords_remove_matches(self):
        probe = _Probe(_config())
        probe.pages = [_page()]
        assert _run(probe, TenderFilters(excluded_keywords=["développement"])).items_found == 0

    def test_a_filtered_out_run_is_distinguishable_from_a_blind_one(self):
        """`items_found: 0` is ambiguous on its own — it reads the same whether
        the filters matched nothing or the selectors broke and we saw nothing.
        `records_parsed` is what separates the two, and an operator staring at a
        quiet source needs that distinction before anything else."""
        probe = _Probe(_config())
        probe.pages = [_page()]
        probe.records_per_page = 2

        outcome = _run(probe, TenderFilters(keywords=["carburant"]))

        assert outcome.items_found == 0            # nothing survived the filter
        assert outcome.records_parsed == 2         # but the selectors did work
        assert outcome.items_filtered_out == 2

    def test_an_unfiltered_run_reports_no_filtering(self):
        probe = _Probe(_config())
        probe.pages = [_page()]
        probe.records_per_page = 2

        outcome = _run(probe)

        assert outcome.records_parsed == 2
        assert outcome.items_filtered_out == 0


class TestRegistry:
    def test_configured_connectors_are_discovered(self):
        registry = get_registry()
        registry.load(force=True)
        keys = registry.keys()
        assert "tuneps" in keys
        assert "j360" in keys

    def test_j360_is_unavailable_without_credentials(self, monkeypatch):
        for var in (
            "SMARTTENDER_CONNECTOR_J360_USERNAME",
            "SMARTTENDER_CONNECTOR_J360_PASSWORD",
            "SMARTTENDER_CONNECTOR_J360_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)

        info = get_registry().describe("j360")
        assert info.available is False
        assert info.unavailable_reason == "credentials_missing"

    def test_j360_is_gated_on_a_captured_session_not_env_vars(self, monkeypatch):
        """Setting username/password is NOT enough: J360 authenticates with a
        browser-captured session, because its login is behind an anti-bot
        challenge. Env credentials only exist to re-capture it interactively."""
        monkeypatch.setenv("SMARTTENDER_CONNECTOR_J360_USERNAME", "buyer@example.tn")
        monkeypatch.setenv("SMARTTENDER_CONNECTOR_J360_PASSWORD", "subscription-password")

        info = get_registry().describe("j360")
        assert info.has_credentials is False
        assert info.available is False
        assert info.unavailable_reason == "credentials_missing"

    def test_j360_becomes_available_once_a_session_is_captured(self, monkeypatch, tmp_path):
        import json as json_module

        from app.connectors.config import load_connector_config

        session = tmp_path / "j360-session.json"
        session.write_text(
            json_module.dumps(
                {
                    "storage_state": {
                        "cookies": [{"name": "sessionid", "value": "abc"}],
                        "origins": [],
                    },
                    "headers": {},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("SMARTTENDER_CONNECTOR_J360_SESSION", str(session))

        # The config resolves the session path; point it at our fixture.
        config = load_connector_config("j360")
        config.raw["auth"]["session_file"] = str(session)
        assert config.has_credentials() is True

    def test_missing_credentials_name_the_command_that_fixes_it(self):
        """For a session-based source, "credentials_missing" must name the
        command, not an env var an operator would set in vain."""
        info = get_registry().describe("j360")
        assert any("capture-login j360" in m for m in info.missing_credentials)

    def test_tuneps_is_public_and_needs_no_credentials(self):
        """The /portail/offres listing is public; a certificate is only needed
        to *bid*, not to browse. So TUNEPS is available with no credentials."""
        info = get_registry().describe("tuneps")
        assert info.requires_credentials is False
        assert info.available is True
        assert load_connector_config("tuneps").strategy is FetchStrategy.DYNAMIC

    def test_tuneps_allows_its_incomplete_certificate_chain(self):
        """TUNEPS serves a chain missing its intermediate; the connector opts
        into accepting it, but only for this one source."""
        assert load_connector_config("tuneps").allow_insecure_tls is True
        # The exception is not a default — no other connector sets it.
        assert load_connector_config("j360").allow_insecure_tls is False
        assert load_connector_config("fixture").allow_insecure_tls is False

    def test_mtls_config_machinery_resolves_a_certificate(self):
        """The client-certificate support still works for any future mTLS
        source, tested directly against a synthetic config."""
        from app.connectors.config import ConnectorConfig

        config = ConnectorConfig(
            key="mtls-probe",
            name="Probe",
            enabled=True,
            strategy=FetchStrategy.API,
            base_url="https://probe.example",
            raw={
                "auth": {
                    "mode": "client_certificate",
                    "credentials_env": {
                        "cert_path": "PROBE_CERT",
                        "key_path": "PROBE_KEY",
                        "key_password": "PROBE_PIN",
                    },
                }
            },
            http={},
        )
        assert config.requires_credentials is True
        assert config.client_certificate() is None   # nothing configured yet

    def test_a_certificate_path_that_does_not_exist_is_treated_as_missing(
        self, monkeypatch
    ):
        """A typo must skip the source, not surface as an opaque SSL error."""
        from app.connectors.config import ConnectorConfig

        monkeypatch.setenv("PROBE_CERT", "/nope/cert.pem")
        monkeypatch.setenv("PROBE_KEY", "/nope/key.pem")
        config = ConnectorConfig(
            key="mtls-probe", name="Probe", enabled=True,
            strategy=FetchStrategy.API, base_url="https://probe.example",
            raw={"auth": {"mode": "client_certificate", "credentials_env": {
                "cert_path": "PROBE_CERT", "key_path": "PROBE_KEY"}}},
            http={},
        )
        assert config.client_certificate() is None
        assert config.has_credentials() is False

    def test_a_certificate_passphrase_is_carried_through(self, monkeypatch, tmp_path):
        from app.connectors.config import ConnectorConfig

        cert = tmp_path / "c.pem"
        key = tmp_path / "k.pem"
        cert.write_text("cert")
        key.write_text("key")
        monkeypatch.setenv("PROBE_CERT", str(cert))
        monkeypatch.setenv("PROBE_KEY", str(key))
        monkeypatch.setenv("PROBE_PIN", "pin-1234")
        config = ConnectorConfig(
            key="mtls-probe", name="Probe", enabled=True,
            strategy=FetchStrategy.API, base_url="https://probe.example",
            raw={"auth": {"mode": "client_certificate", "credentials_env": {
                "cert_path": "PROBE_CERT", "key_path": "PROBE_KEY",
                "key_password": "PROBE_PIN"}}},
            http={},
        )
        assert config.client_certificate() == (str(cert), str(key), "pin-1234")

    def test_unknown_keys_are_skipped_not_fatal(self):
        runnable, skipped = get_registry().resolve_requested(["fixture", "does-not-exist"])
        assert "fixture" in runnable
        assert "does-not-exist" in skipped

    def test_an_empty_request_resolves_to_every_available_source(self):
        runnable, skipped = get_registry().resolve_requested(None)
        # TUNEPS is public (available); J360 needs a subscription the test env
        # lacks. So fixture and tuneps are runnable here.
        assert "fixture" in runnable
        assert "tuneps" in runnable
        assert skipped == []

    def test_configs_carry_a_checksum(self):
        config = load_connector_config("tuneps")
        assert len(config.checksum) == 64
        assert config.describe()["key"] == "tuneps"

    def test_a_config_dump_never_leaks_credentials(self, monkeypatch):
        monkeypatch.setenv("SMARTTENDER_CONNECTOR_J360_API_KEY", "super-secret")
        described = load_connector_config("j360").describe()
        assert "super-secret" not in str(described)


class TestFixtureConnectorEndToEnd:
    def test_the_whole_connector_pipeline_runs_offline(self):
        registry = get_registry()
        registry.load(force=True)
        connector = registry.create(
            "fixture", ConnectorContext(trigger=JobTrigger.MANUAL, allow_private_hosts=True)
        )
        outcome = asyncio.run(connector.run(TenderFilters()))

        assert outcome.succeeded is True
        assert outcome.items_found >= 4

        titles = [t.title for t in outcome.tenders]
        assert any("gestion documentaire" in title for title in titles)

        first = next(t for t in outcome.tenders if "gestion documentaire" in t.title)
        assert first.buyer == "Ministère des Technologies de la Communication"
        assert first.deadline is not None
        assert first.estimated_budget is not None
        assert first.canonical_url

    def test_filters_narrow_the_result(self):
        registry = get_registry()
        registry.load(force=True)
        connector = registry.create("fixture", ConnectorContext(allow_private_hosts=True))
        outcome = asyncio.run(
            connector.run(TenderFilters(keywords=["audit"], keywords_any=True))
        )
        assert outcome.items_found >= 1
        assert all("audit" in t.title.lower() or "audit" in (t.description or "").lower()
                   for t in outcome.tenders)


class TestTunepsNormalisation:
    def _record(self, **fields) -> RawRecord:
        return RawRecord(
            connector_key="tuneps",
            source_url="https://www.tuneps.tn/portail/offres/details?epBidMasterId=133063",
            fields=fields,
        )

    def test_status_is_derived_from_the_deadline(self):
        from datetime import timedelta

        from app.connectors.tuneps.connector import TunepsConnector
        from app.core.enums import TenderStatus
        from app.core.identity import utc_now

        connector = TunepsConnector(load_connector_config("tuneps"))
        # Real TUNEPS listing fields: a clean bidNo reference and the portal's
        # own record id.
        record = self._record(
            title="Acquisition et mise en œuvre d'une solution ERP",
            reference="20260701931",
            external_id="133063",
            deadline=(utc_now() + timedelta(days=30)).strftime("%d/%m/%Y %H:%M"),
        )
        tender = connector.normalize(record)

        assert tender.country == "Tunisie"
        assert tender.status is TenderStatus.OPEN
        assert tender.extra["status_derived_from"] == "deadline"
        assert tender.reference == "20260701931"
        # The portal's record id becomes the dedup/enrichment key.
        assert tender.external_id == "133063"
        assert tender.extra["epBidMasterId"] == "133063"

    def test_an_arabic_only_title_is_accepted(self):
        """Many real TUNEPS notices are Arabic-only; that is valid content."""
        from app.connectors.tuneps.connector import TunepsConnector

        connector = TunepsConnector(load_connector_config("tuneps"))
        tender = connector.normalize(
            self._record(title="التزود بمواد كهربائية لسنة 2026", reference="20260701930")
        )
        assert "كهربائية" in tender.title

    def test_a_past_deadline_derives_closed(self):
        from datetime import timedelta

        from app.connectors.tuneps.connector import TunepsConnector
        from app.core.enums import TenderStatus
        from app.core.identity import utc_now

        connector = TunepsConnector(load_connector_config("tuneps"))
        record = self._record(
            title="Un marché déjà clôturé",
            deadline=(utc_now() - timedelta(days=5)).strftime("%d/%m/%Y"),
        )
        assert connector.normalize(record).status is TenderStatus.CLOSED

    def test_each_row_gets_a_unique_source_url(self, page_bytes):
        """Regression: rows carry no detail link, so without a synthetic URL a
        whole page of tenders would collapse to one in in-run dedup."""
        from app.connectors.models import FetchedPage
        from app.connectors.tuneps.connector import TunepsConnector

        html = b"""
        <table><tbody>
          <tr class="mat-row">
            <td class="cdk-column-bidNo">20260701931</td>
            <td class="cdk-column-bidInstNm">Societe A</td>
            <td class="cdk-column-publicDt">28/07/2026</td>
            <td class="cdk-column-bidNmFr">Marche un</td>
            <td class="cdk-column-bdRecvEndDt">28/08/2026 09:00</td>
            <td class="cdk-column-epBidMasterId">133063</td>
          </tr>
          <tr class="mat-row">
            <td class="cdk-column-bidNo">20260701930</td>
            <td class="cdk-column-bidInstNm">Commune B</td>
            <td class="cdk-column-publicDt">28/07/2026</td>
            <td class="cdk-column-bidNmFr">Marche deux</td>
            <td class="cdk-column-bdRecvEndDt">27/08/2026 10:00</td>
            <td class="cdk-column-epBidMasterId">133061</td>
          </tr>
        </tbody></table>
        """
        connector = TunepsConnector(load_connector_config("tuneps"))
        page = FetchedPage(url="https://www.tuneps.tn/portail/offres#page=1",
                           status_code=200, content=html)
        records = connector.parse(page)

        assert len(records) == 2
        urls = {r.source_url for r in records}
        assert len(urls) == 2                       # distinct, not collapsed
        assert "133063" in records[0].source_url
