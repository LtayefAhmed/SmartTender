"""Parser and normaliser tests, against saved page fixtures.

These are the regression tests that protect against the platform's most
expensive failure mode: a portal changes its markup, the connector keeps
returning HTTP 200 with zero rows, and nobody notices for a week.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.connectors.parsing.normalizers import (
    detect_currency,
    normalize_date,
    normalize_email,
    normalize_money,
    normalize_text,
    parse_bool,
    strip_patterns,
)
from app.connectors.parsing.selectors import (
    SelectorEngine,
    extract_json_path,
    parse_html,
)
from app.core.exceptions import SelectorBrokenError


@pytest.fixture()
def listing(page_bytes):
    return SelectorEngine(parse_html(page_bytes("listing_page_1.html")))


class TestSelectorEngine:
    def test_extracts_text_from_the_first_match(self, listing):
        rows = listing.nodes("table.tenders tbody tr")
        assert len(rows) == 3
        assert rows[0].get("td.ref") == "AO 12/2026"

    def test_extracts_an_attribute(self, listing):
        rows = listing.nodes("table.tenders tbody tr")
        assert rows[0].get("td.title a@href") == "detail_ao-12-2026.html"

    def test_falls_back_to_the_next_alternative(self, listing):
        # The old selector no longer matches; the fallback does. This is the
        # mechanism that lets a portal redesign be survived with a YAML edit.
        rows = listing.nodes("table.tenders tbody tr")
        assert rows[0].get("td.reference-old, td.ref") == "AO 12/2026"

    def test_alternatives_do_not_union_their_results(self, listing):
        # Both alternatives match the same rows; returning six would silently
        # double every listing.
        assert len(listing.get_all("td.ref, td.ref")) == 3

    def test_commas_inside_brackets_do_not_split_the_selector(self):
        engine = SelectorEngine(parse_html('<div data-x="a,b">value</div>'))
        assert engine.get('div[data-x="a,b"]') == "value"

    def test_missing_selector_returns_the_default(self, listing):
        assert listing.get("td.does-not-exist", "fallback") == "fallback"

    def test_guard_selector_raises_when_markup_changes(self, page_bytes):
        engine = SelectorEngine(parse_html(page_bytes("tuneps_listing_broken.html")))
        with pytest.raises(SelectorBrokenError) as excinfo:
            engine.require(
                "table.table-resultats, div.liste-avis",
                what="the results listing",
                url="https://www.tuneps.tn/fr/appels-offres",
            )
        assert excinfo.value.alerting is True
        assert "selector" in excinfo.value.context

    def test_guard_selector_passes_on_the_known_markup(self, page_bytes):
        engine = SelectorEngine(parse_html(page_bytes("tuneps_listing.html")))
        # The real TUNEPS listing is a Material table; its guard selector matches.
        engine.require("table", what="the results listing")

    def test_extract_applies_a_whole_mapping(self, listing):
        row = listing.nodes("table.tenders tbody tr")[1]
        fields = row.extract({"reference": "td.ref", "buyer": "td.buyer"})
        assert fields["reference"] == "AO 13/2026"
        assert fields["buyer"] == "Banque Centrale de Tunisie"


class TestTunepsSelectors:
    """Pins the selectors in ``config/connectors/tuneps.yaml`` to real markup."""

    def test_configured_selectors_match_the_snapshot(self, page_bytes):
        from app.connectors.config import load_connector_config

        config = load_connector_config("tuneps")
        engine = SelectorEngine(parse_html(page_bytes("tuneps_listing.html")))

        engine.require(config.selectors["list_container"], what="listing")
        rows = engine.nodes(config.selectors["list_item"])
        assert len(rows) == 2

        item = config.selectors["item"]
        # The real listing: a clean bidNo reference, the buyer, an
        # Arabic-or-French title, and the portal's own record id.
        assert rows[0].get(item["reference"]) == "20260701931"
        assert "assurances" in rows[0].get(item["title"])
        assert rows[0].get(item["buyer"]) == "Société de Promotion de Logements Sociaux"
        assert rows[0].get(item["deadline"]) == "28/08/2026 09:00"
        assert rows[0].get(item["external_id"]) == "133063"
        # Arabic-only titles are common and legitimate.
        assert "كهربائية" in rows[1].get(item["title"])


class TestDateNormalisation:
    def test_configured_format_wins(self):
        parsed = normalize_date("28/08/2026", formats=["%d/%m/%Y"])
        assert parsed == datetime(2026, 8, 28, tzinfo=timezone.utc)

    def test_datetime_with_time_component(self):
        parsed = normalize_date("30/09/2026 12:00", formats=["%d/%m/%Y %H:%M"])
        assert parsed.hour == 12

    def test_iso_8601(self):
        assert normalize_date("2026-09-30T12:00:00Z").month == 9

    def test_french_textual_dates(self):
        assert normalize_date("15 janvier 2026") == datetime(2026, 1, 15, tzinfo=timezone.utc)
        assert normalize_date("1er décembre 2026").month == 12

    def test_english_textual_dates(self):
        assert normalize_date("15 January 2026").month == 1

    def test_naive_dates_are_localised_to_the_portal_timezone(self):
        # Africa/Tunis is UTC+1, so local midnight is 23:00 UTC the day before.
        # Assuming UTC here would shift a submission deadline by a whole day.
        parsed = normalize_date("28/08/2026", formats=["%d/%m/%Y"], tz="Africa/Tunis")
        assert parsed == datetime(2026, 8, 27, 23, 0, tzinfo=timezone.utc)

    def test_unparseable_input_returns_none_rather_than_raising(self):
        assert normalize_date("bientôt") is None
        assert normalize_date(None) is None
        assert normalize_date("") is None


class TestMoneyNormalisation:
    def test_french_locale(self):
        amount, currency = normalize_money(
            "1 250 000,000 TND", decimal_separator=",", thousands_separator=" "
        )
        assert amount == Decimal("1250000.00")
        assert currency == "TND"

    def test_us_locale(self):
        amount, _ = normalize_money(
            "1,250,000.50", decimal_separator=".", thousands_separator=","
        )
        assert amount == Decimal("1250000.50")

    def test_millions_multiplier(self):
        amount, _ = normalize_money("2,5 millions EUR", decimal_separator=",")
        assert amount == Decimal("2500000.00")

    def test_currency_symbols(self):
        assert normalize_money("450 000 €")[1] == "EUR"
        assert normalize_money("12 000 DT")[1] == "TND"

    def test_default_currency_when_absent(self):
        assert normalize_money("450000", default_currency="TND")[1] == "TND"

    def test_unparseable_returns_none(self):
        amount, currency = normalize_money("montant non communiqué", default_currency="TND")
        assert amount is None
        assert currency == "TND"

    def test_numeric_input_passes_through(self):
        assert normalize_money(1500)[0] == Decimal("1500")

    def test_detect_currency_ignores_common_words(self):
        assert detect_currency("THE AND FOR") is None


class TestTextNormalisation:
    def test_whitespace_is_collapsed(self):
        assert normalize_text("  a\n\t b  ") == "a b"

    def test_zero_width_characters_are_removed(self):
        assert normalize_text("a​b") == "ab"

    def test_empty_becomes_none(self):
        assert normalize_text("   ") is None
        assert normalize_text(None) is None

    def test_boilerplate_patterns_are_stripped(self):
        result = strip_patterns(
            "Objet : Développement web (nouvelle fenêtre)",
            [r"^\s*Objet\s*:\s*", r"\s*\(nouvelle fenêtre\)\s*$"],
        )
        assert result == "Développement web"

    def test_email_extraction(self):
        assert normalize_email("mailto:Contact@Portal.TN?subject=x") == "contact@portal.tn"
        assert normalize_email("Écrire à contact@portal.tn svp") == "contact@portal.tn"
        assert normalize_email("pas d'email") is None

    def test_bool_parsing_handles_french(self):
        assert parse_bool("oui") is True
        assert parse_bool("non") is False
        assert parse_bool("peut-être", default=True) is True


class TestJsonPath:
    PAYLOAD = {
        "data": [{"id": 1, "buyer": {"name": "STEG"}}],
        "meta": {"total": 1, "next_cursor": "abc"},
        "documents": [{"url": "u1", "name": "n1"}, {"url": "u2", "name": "n2"}],
    }

    def test_dotted_path(self):
        assert extract_json_path(self.PAYLOAD, "meta.total") == 1

    def test_index_into_a_list(self):
        assert extract_json_path(self.PAYLOAD, "data.0.buyer.name") == "STEG"

    def test_list_projection(self):
        assert extract_json_path(self.PAYLOAD, "documents[].url") == ["u1", "u2"]

    def test_missing_path_returns_default(self):
        assert extract_json_path(self.PAYLOAD, "meta.nope", "fallback") == "fallback"
        assert extract_json_path(self.PAYLOAD, None) is None
