"""Core kernel: canonicalisation, hashing, security primitives, config."""

from __future__ import annotations

import pytest

from app.core.config import deep_merge, load_yaml_config
from app.core.exceptions import (
    DuplicateTenderError,
    SmartTenderError,
    SourceUnavailableError,
    ValidationError,
)
from app.core.identity import (
    canonicalize_url,
    content_fingerprint,
    deterministic_uuid,
    idempotency_key,
    normalize_text,
)
from app.core.security import (
    assert_public_url,
    redact,
    redact_url,
    safe_object_key,
    sanitize_filename,
    verify_api_key,
)


class TestCanonicalizeUrl:
    def test_strips_tracking_and_session_parameters(self):
        noisy = (
            "https://Portal.Example.TN:443/avis/12?utm_source=mail&sessionid=abc"
            "&ref=42#section-2"
        )
        assert (
            canonicalize_url(noisy, strip_params=["utm_source", "sessionid", "ref"])
            == "https://portal.example.tn/avis/12"
        )

    def test_two_spellings_of_the_same_page_collide(self):
        strip = ["utm_source", "sessionid"]
        first = canonicalize_url(
            "https://portal.example.tn/avis/12?b=2&a=1&utm_source=x", strip_params=strip
        )
        second = canonicalize_url(
            "https://PORTAL.example.tn/avis/12/?a=1&b=2&sessionid=zz#top", strip_params=strip
        )
        assert first == second

    def test_meaningful_parameters_are_preserved(self):
        # Stripping a parameter that actually selects the document would merge
        # two genuinely different tenders.
        url = "https://portal.example.tn/avis?id=12&lot=3"
        assert "id=12" in canonicalize_url(url)
        assert "lot=3" in canonicalize_url(url)

    def test_empty_input_is_safe(self):
        assert canonicalize_url("") == ""

    def test_a_hash_route_is_part_of_the_address(self):
        """Single-page apps put the whole route after the `#`. Treating that as
        a discardable anchor gives every record on the site one canonical URL,
        and duplicate detection then merges an entire portal into one tender —
        while still reporting success. J360 addresses announcements this way."""
        urls = [
            "https://app.j360.info/#/announce/55822711",
            "https://app.j360.info/#/announce/55822811",
            "https://app.j360.info/#/announce/55974864",
        ]
        canonical = {canonicalize_url(u) for u in urls}

        assert len(canonical) == 3
        assert "55822711" in canonicalize_url(urls[0])

    def test_a_hashbang_route_is_kept_too(self):
        assert "/item/7" in canonicalize_url("https://example.com/app#!/item/7")

    def test_a_plain_anchor_is_still_noise(self):
        """`#results` names a spot on a page already identified by its path."""
        assert canonicalize_url("https://portal.example.tn/avis/12#results") == (
            "https://portal.example.tn/avis/12"
        )


class TestHashing:
    def test_text_hash_ignores_formatting_noise(self):
        first = content_fingerprint(text="Appel d'offres:  DÉVELOPPEMENT   Web!")
        second = content_fingerprint(text="appel d offres developpement web")
        assert first["text_sha256"] == second["text_sha256"]

    def test_raw_hash_distinguishes_different_bytes(self):
        assert (
            content_fingerprint(raw=b"a")["raw_sha256"]
            != content_fingerprint(raw=b"b")["raw_sha256"]
        )

    def test_deterministic_uuid_is_stable(self):
        assert deterministic_uuid("tender", "doc-1") == deterministic_uuid("tender", "doc-1")
        assert deterministic_uuid("tender", "doc-1") != deterministic_uuid("tender", "doc-2")

    def test_idempotency_key_is_stable_and_short(self):
        key = idempotency_key("job", "connector", "page-1")
        assert key == idempotency_key("job", "connector", "page-1")
        assert len(key) == 32

    def test_normalize_text_strips_accents_and_punctuation(self):
        assert normalize_text("Marché  Public, N°12!") == "marche public n 12"


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../etc/passwd",
            "..\\..\\windows\\system32\\config",
            "/absolute/path/file.pdf",
            "C:\\Users\\admin\\secret.pdf",
        ],
    )
    def test_directory_components_are_removed(self, hostile):
        result = sanitize_filename(hostile)
        assert "/" not in result
        assert "\\" not in result
        assert ".." not in result

    def test_accents_are_transliterated_not_dropped(self):
        assert sanitize_filename("Cahier_des_charges_marché.pdf") == "Cahier_des_charges_marche.pdf"

    def test_windows_device_names_are_neutralised(self):
        assert sanitize_filename("CON.pdf") == "CON_file.pdf"

    def test_empty_input_falls_back(self):
        assert sanitize_filename(None) == "document"
        assert sanitize_filename("   ") == "document"

    def test_length_is_bounded(self):
        assert len(sanitize_filename("x" * 500 + ".pdf")) <= 130


class TestObjectKeys:
    def test_segments_are_joined(self):
        assert safe_object_key("tenders", "2026", "file.pdf") == "tenders/2026/file.pdf"

    def test_traversal_is_refused(self):
        with pytest.raises(ValidationError):
            safe_object_key("tenders", "..", "etc")

    def test_empty_key_is_refused(self):
        with pytest.raises(ValidationError):
            safe_object_key("", "  ")


class TestRedaction:
    def test_nested_secrets_are_masked(self):
        payload = {
            "connector": "j360",
            "auth": {"mode": "session_login", "username": "bob", "password": "hunter2"},
            "headers": [{"Authorization": "Bearer x"}],
        }
        result = redact(payload)

        assert result["connector"] == "j360"
        assert result["auth"]["password"] != "hunter2"
        assert result["auth"]["username"] != "bob"
        assert result["headers"][0]["Authorization"] != "Bearer x"

    def test_containers_keep_their_diagnostic_structure(self):
        """Masking a whole `auth` block would hide the mode and the endpoint —
        exactly the fields that make an auth failure diagnosable."""
        result = redact({"auth": {"mode": "oauth2", "token": "xyz"}})
        assert result["auth"]["mode"] == "oauth2"
        assert result["auth"]["token"] != "xyz"

    def test_url_userinfo_and_secret_params_are_masked(self):
        cleaned = redact_url("https://bob:hunter2@portal.tn/x?api_key=abc&page=2")
        assert "hunter2" not in cleaned
        assert "abc" not in cleaned
        assert "page=2" in cleaned


class TestApiKeyVerification:
    def test_accepts_a_configured_key(self):
        assert verify_api_key("k2", ["k1", "k2"]) is True

    def test_rejects_unknown_and_empty(self):
        assert verify_api_key("nope", ["k1"]) is False
        assert verify_api_key(None, ["k1"]) is False
        assert verify_api_key("k1", []) is False


class TestSsrfGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8000/admin",
            "http://10.0.0.5/internal",
            "http://localhost/x",
            "file:///etc/passwd",
        ],
    )
    def test_internal_targets_are_refused(self, url):
        with pytest.raises(ValidationError):
            assert_public_url(url)

    def test_public_urls_pass(self):
        assert assert_public_url("https://www.tuneps.tn/fr/appels-offres")

    def test_private_hosts_can_be_allowed_explicitly(self):
        assert assert_public_url("http://127.0.0.1:9000/x", allow_private=True)


class TestExceptionHierarchy:
    def test_retry_metadata_drives_policy(self):
        assert SourceUnavailableError("down").retryable is True
        assert DuplicateTenderError("dup").terminal is True
        assert DuplicateTenderError("dup").alerting is False
        assert ValidationError("bad").retryable is False

    def test_context_is_carried_and_serialisable(self):
        exc = SourceUnavailableError("down", connector="tuneps", url="https://x.tn")
        payload = exc.to_dict()
        assert payload["code"] == "source_unavailable"
        assert payload["context"]["connector"] == "tuneps"

    def test_every_error_is_a_smarttender_error(self):
        for exc in (SourceUnavailableError("a"), ValidationError("b"), DuplicateTenderError("c")):
            assert isinstance(exc, SmartTenderError)


class TestConfig:
    def test_yaml_is_loaded_and_isolated_between_callers(self):
        first = load_yaml_config("scoring")
        first["weights"]["field_of_work"] = 999
        second = load_yaml_config("scoring")
        # A caller mutating its copy must not corrupt anyone else's view.
        assert second["weights"]["field_of_work"] != 999

    def test_connector_overrides_merge_onto_global_policy(self):
        merged = deep_merge(
            {"retry": {"max_attempts": 4, "jitter_ratio": 0.25}, "timeouts": {"read": 30}},
            {"retry": {"max_attempts": 2}},
        )
        assert merged["retry"]["max_attempts"] == 2
        assert merged["retry"]["jitter_ratio"] == 0.25   # untouched key survives
        assert merged["timeouts"]["read"] == 30

    def test_lists_are_replaced_not_merged(self):
        merged = deep_merge({"pool": ["a", "b"]}, {"pool": ["c"]})
        assert merged["pool"] == ["c"]
