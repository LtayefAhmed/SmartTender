"""cURL import and captured-session handling.

These cover the two pieces that turn "I found the API in DevTools" into a
working connector: parsing the copied request, and reusing a browser-captured
session for the fast crawl.
"""

from __future__ import annotations

import json

import pytest

from app.connectors.curl_import import (
    detect_pagination,
    infer_item_mapping,
    parse_curl,
    suggest_config,
)
from app.core.exceptions import AuthenticationError

# A realistic Chrome "Copy as cURL", including the line continuations and the
# cookie/CSRF headers a Django backend sets.
CHROME_CURL = r"""curl 'https://app.j360.info/api/v1/tenders/?page=2&page_size=50&country=TN' \
  -H 'accept: application/json, text/plain, */*' \
  -H 'accept-language: fr-FR,fr;q=0.9' \
  -H 'cookie: sessionid=abc123def; csrftoken=xyz789' \
  -H 'referer: https://app.j360.info/' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0' \
  -H 'x-csrftoken: xyz789' \
  -H 'x-requested-with: XMLHttpRequest' \
  --compressed"""

DRF_RESPONSE = {
    "count": 4213,
    "next": "https://app.j360.info/api/v1/tenders/?page=3&page_size=50&country=TN",
    "previous": "https://app.j360.info/api/v1/tenders/?page=1&page_size=50&country=TN",
    "results": [
        {
            "id": 918273,
            "reference": "AO-2026-4471",
            "title": "Développement d'une plateforme de gestion documentaire",
            "description": "Conception, développement et maintenance applicative.",
            "buyer": {"name": "Ministère des Technologies", "country": "TN"},
            "publication_date": "2026-07-20T09:00:00Z",
            "deadline": "2026-08-28T12:00:00Z",
            "estimated_value": 1250000,
            "currency": "TND",
            "cpv": ["72200000"],
            "url": "https://app.j360.info/#/tender/918273",
        }
    ],
}


class TestParseCurl:
    def test_it_extracts_the_request(self):
        parsed = parse_curl(CHROME_CURL)
        assert parsed.method == "GET"
        assert parsed.base_url == "https://app.j360.info"
        assert parsed.path == "/api/v1/tenders/"
        assert parsed.query["page"] == "2"
        assert parsed.query["country"] == "TN"

    def test_replayable_headers_are_kept(self):
        headers = parse_curl(CHROME_CURL).headers
        assert "user-agent" in {k.lower() for k in headers}
        assert "accept" in {k.lower() for k in headers}

    def test_secrets_are_separated_from_replayable_headers(self):
        """Cookies and CSRF tokens belong in a session file, never in a config
        file that gets committed."""
        parsed = parse_curl(CHROME_CURL)

        header_names = {k.lower() for k in parsed.headers}
        assert "cookie" not in header_names
        assert "x-csrftoken" not in header_names

        assert parsed.cookies["sessionid"] == "abc123def"
        assert any("csrf" in s.lower() for s in parsed.secret_headers)

    def test_the_summary_never_leaks_secret_values(self):
        rendered = str(parse_curl(CHROME_CURL).describe())
        assert "abc123def" not in rendered
        assert "xyz789" not in rendered
        # ...but it does name what it found, so the operator knows.
        assert "sessionid" in rendered

    def test_a_post_with_a_json_body(self):
        parsed = parse_curl(
            "curl 'https://api.example.com/search' -X POST "
            "-H 'content-type: application/json' "
            """--data-raw '{"query":"dev","page":1}'"""
        )
        assert parsed.method == "POST"
        assert parsed.body == {"query": "dev", "page": 1}

    def test_method_is_inferred_from_a_body(self):
        parsed = parse_curl("curl 'https://api.example.com/x' --data-raw 'a=1'")
        assert parsed.method == "POST"

    def test_a_non_curl_string_is_rejected_clearly(self):
        with pytest.raises(ValueError, match="does not look like a cURL"):
            parse_curl("GET /api/v1/tenders HTTP/1.1")

    def test_a_missing_url_is_rejected(self):
        with pytest.raises(ValueError, match="No URL"):
            parse_curl("curl -H 'accept: application/json'")


class TestPaginationDetection:
    def test_a_drf_envelope_selects_next_url(self):
        """Following the server's own `next` survives parameter renames and
        page-size caps, so it wins over rebuilding parameters."""
        plan = detect_pagination({"page": "2", "page_size": "50"}, DRF_RESPONSE)
        assert plan["mode"] == "next_url"
        assert plan["next_response_path"] == "next"

    def test_page_parameters_without_a_drf_envelope(self):
        plan = detect_pagination({"page": "3", "per_page": "25"}, {"items": []})
        assert plan["mode"] == "page"
        assert plan["page_param"] == "page"
        assert plan["page_size"] == 25

    def test_offset_pagination(self):
        plan = detect_pagination({"offset": "100", "limit": "50"}, {"data": []})
        assert plan["mode"] == "offset"
        assert plan["offset_param"] == "offset"

    def test_cursor_pagination(self):
        plan = detect_pagination({"cursor": "abc"}, {"data": []})
        assert plan["mode"] == "cursor"


class TestFieldMapping:
    def test_it_maps_the_obvious_fields(self):
        mapping = infer_item_mapping(DRF_RESPONSE["results"][0])
        assert mapping["title"] == "title"
        assert mapping["reference"] == "reference"
        assert mapping["deadline"] == "deadline"
        assert mapping["external_id"] == "id"

    def test_it_reaches_into_nested_objects(self):
        mapping = infer_item_mapping(DRF_RESPONSE["results"][0])
        assert mapping["buyer"] == "buyer.name"

    def test_an_empty_record_maps_nothing(self):
        assert infer_item_mapping({}) == {}


class TestSuggestConfig:
    def test_it_produces_loadable_yaml(self):
        import yaml

        text = suggest_config("j360", parse_curl(CHROME_CURL), DRF_RESPONSE)
        config = yaml.safe_load(text)

        assert config["key"] == "j360"
        assert config["base_url"] == "https://app.j360.info"
        assert config["endpoints"]["search"] == "/api/v1/tenders/"
        assert config["pagination"]["mode"] == "next_url"
        assert config["response_mapping"]["items_path"] == "results"
        assert config["response_mapping"]["item"]["title"] == "title"

    def test_it_defaults_to_a_captured_session(self):
        import yaml

        config = yaml.safe_load(suggest_config("j360", parse_curl(CHROME_CURL), DRF_RESPONSE))
        assert config["auth"]["mode"] == "browser_session"

    def test_the_generated_config_never_contains_secrets(self):
        text = suggest_config("j360", parse_curl(CHROME_CURL), DRF_RESPONSE)
        assert "abc123def" not in text
        assert "xyz789" not in text
        # ...and it says where they should go instead.
        assert "capture-login" in text

    def test_it_is_conservative_about_rate_limits(self):
        import yaml

        config = yaml.safe_load(suggest_config("j360", parse_curl(CHROME_CURL), DRF_RESPONSE))
        assert config["http"]["concurrency"]["per_connector"] == 1
        assert config["http"]["rate_limit"]["requests_per_second"] <= 1


class TestSessionStore:
    def _state(self, **overrides):
        state = {
            "cookies": [
                {"name": "sessionid", "value": "abc123", "domain": "app.j360.info"},
                {"name": "csrftoken", "value": "xyz789", "domain": "app.j360.info"},
            ],
            "origins": [{"origin": "https://app.j360.info"}],
        }
        state.update(overrides)
        return state

    def test_round_trip(self, tmp_path):
        from app.connectors.http.session_store import load_session, save_session

        path = tmp_path / "s.json"
        save_session(path, self._state(), headers={"User-Agent": "Mozilla/5.0 probe"})

        session = load_session(path)
        assert session.cookies["sessionid"] == "abc123"
        assert session.headers["User-Agent"] == "Mozilla/5.0 probe"
        assert session.age_hours is not None and session.age_hours < 1

    def test_a_missing_session_names_the_fix(self, tmp_path):
        from app.connectors.http.session_store import load_session

        with pytest.raises(AuthenticationError) as excinfo:
            load_session(tmp_path / "nope.json")
        assert "capture-login" in excinfo.value.message

    def test_a_stale_session_is_refused(self, tmp_path):
        """Crawling with cookies about to lapse writes empty pages halfway
        through; refusing up front is cheaper to diagnose."""
        from datetime import datetime, timedelta, timezone

        from app.connectors.http.session_store import load_session

        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(
                {
                    "storage_state": self._state(),
                    "headers": {},
                    "captured_at": (
                        datetime.now(timezone.utc) - timedelta(hours=200)
                    ).isoformat(),
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(AuthenticationError, match="old"):
            load_session(path, max_age_hours=72)

    def test_a_cookieless_session_is_refused(self, tmp_path):
        from app.connectors.http.session_store import load_session, save_session

        path = tmp_path / "s.json"
        save_session(path, {"cookies": [], "origins": []})
        with pytest.raises(AuthenticationError, match="no cookies"):
            load_session(path)

    def test_the_summary_never_leaks_cookie_values(self, tmp_path):
        from app.connectors.http.session_store import load_session, save_session

        path = tmp_path / "s.json"
        save_session(path, self._state())
        described = str(load_session(path).describe())

        assert "abc123" not in described
        assert "sessionid" in described


class TestJ360Configuration:
    def test_it_is_gated_on_a_captured_session(self):
        from app.connectors.config import load_connector_config

        config = load_connector_config("j360")
        assert config.auth_mode == "browser_session"
        assert config.requires_credentials is True
        # No session captured in the test environment.
        assert config.has_credentials() is False

    def test_the_missing_credential_message_names_the_command(self):
        from app.connectors.config import load_connector_config

        missing = load_connector_config("j360").missing_credentials()
        assert any("capture-login j360" in m for m in missing)

    def test_it_follows_the_servers_next_link(self):
        from app.connectors.config import load_connector_config

        assert load_connector_config("j360").pagination["mode"] == "next_url"

    def test_it_never_retries_an_auth_failure(self):
        """Retrying a 401/403 cannot fix a lapsed session and risks tripping
        account protections."""
        from app.connectors.config import load_connector_config

        statuses = load_connector_config("j360").http_get("retry.retry_on_status")
        assert 401 not in statuses
        assert 403 not in statuses
