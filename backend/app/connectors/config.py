"""Connector configuration: the merge of global HTTP policy and per-source YAML.

A connector never reads YAML itself. It receives a fully-resolved
``ConnectorConfig`` whose ``http`` block is ``config/http.yaml`` with the
connector's own ``http:`` overrides applied on top. That means a source can
tighten its rate limit or lengthen a timeout without restating the other forty
settings, and a global policy change reaches every connector at once.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import orjson

from app.core.config import deep_merge, env_credential, load_yaml_config
from app.core.enums import FetchStrategy, coerce
from app.core.exceptions import ConfigurationError

__all__ = ["ConnectorConfig", "load_connector_config"]


def _get(mapping: dict[str, Any], path: str, default: Any = None) -> Any:
    """Read a dotted path out of a nested mapping."""
    node: Any = mapping
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node is not None else default


@dataclass(slots=True)
class ConnectorConfig:
    """Immutable, fully-resolved configuration for one connector."""

    key: str
    name: str
    enabled: bool
    strategy: FetchStrategy
    base_url: str
    raw: dict[str, Any] = field(default_factory=dict)
    http: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Generic access
    # ------------------------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        return _get(self.raw, path, default)

    def http_get(self, path: str, default: Any = None) -> Any:
        return _get(self.http, path, default)

    # ------------------------------------------------------------------
    # Frequently-used blocks, typed
    # ------------------------------------------------------------------
    @property
    def country(self) -> str | None:
        return self.raw.get("country")

    @property
    def language(self) -> str | None:
        return self.raw.get("language")

    @property
    def endpoints(self) -> dict[str, str]:
        return dict(self.raw.get("endpoints") or {})

    @property
    def selectors(self) -> dict[str, Any]:
        return dict(self.raw.get("selectors") or {})

    @property
    def pagination(self) -> dict[str, Any]:
        return dict(self.raw.get("pagination") or {})

    @property
    def filter_mapping(self) -> dict[str, str | None]:
        return dict(self.raw.get("filter_mapping") or {})

    @property
    def filter_values(self) -> dict[str, dict[str, str]]:
        return dict(self.raw.get("filter_values") or {})

    @property
    def parsing(self) -> dict[str, Any]:
        return dict(self.raw.get("parsing") or {})

    @property
    def response_mapping(self) -> dict[str, Any]:
        return dict(self.raw.get("response_mapping") or {})

    @property
    def required_fields(self) -> list[str]:
        return list(self.raw.get("required_fields") or ["title"])

    @property
    def documents_policy(self) -> dict[str, Any]:
        return dict(self.raw.get("documents") or {})

    @property
    def health_policy(self) -> dict[str, Any]:
        return dict(self.raw.get("health") or {})

    @property
    def auth(self) -> dict[str, Any]:
        return dict(self.raw.get("auth") or {})

    @property
    def allow_insecure_tls(self) -> bool:
        """Whether to accept an incomplete/self-signed certificate chain.

        A per-connector opt-in for a known portal, never a default. TUNEPS
        serves a chain missing its intermediate; we read only its public
        listing, so the exception is contained to that source.
        """
        return bool((self.raw.get("tls") or {}).get("allow_insecure", False))

    @property
    def environments(self) -> list[str] | None:
        """Environments this connector may run in; ``None`` means all of them."""
        value = self.raw.get("environments")
        return list(value) if value else None

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------
    @property
    def requires_credentials(self) -> bool:
        return bool(self.auth.get("credentials_env")) or self.auth_mode == "browser_session"

    @property
    def auth_mode(self) -> str:
        return str(self.auth.get("mode") or "").lower()

    def session_file(self) -> Any:
        """Path to this connector's captured browser session, if it uses one."""
        if self.auth_mode != "browser_session":
            return None
        from app.connectors.http.session_store import session_path

        return session_path(self.key, self.auth.get("session_file"))

    def credentials(self) -> dict[str, str]:
        """Resolve credentials from the environment.

        Values are read fresh on every call and never cached on the config
        object, so a rotated secret takes effect on the next run and a config
        dump can never leak one.
        """
        mapping = self.auth.get("credentials_env") or {}
        resolved: dict[str, str] = {}
        for logical_name, env_var in mapping.items():
            value = env_credential(str(env_var))
            if value:
                resolved[logical_name] = value
        return resolved

    def client_certificate(self) -> tuple[str, str] | tuple[str, str, str] | None:
        """Resolve a TLS client certificate for mutual-TLS sources.

        Returns the tuple httpx expects — ``(cert, key)`` or
        ``(cert, key, password)`` — or ``None`` when this source does not use
        one. Paths are validated here so a typo surfaces as
        ``credentials_missing`` (the source is skipped) rather than as an
        opaque SSL error in the middle of a run.
        """
        resolved = self.credentials()
        cert_path = resolved.get("cert_path")
        key_path = resolved.get("key_path")
        if not cert_path or not key_path:
            return None

        from pathlib import Path

        if not Path(cert_path).is_file() or not Path(key_path).is_file():
            return None

        password = resolved.get("key_password")
        return (cert_path, key_path, password) if password else (cert_path, key_path)

    def has_credentials(self) -> bool:
        """True when enough credentials are present for the declared auth mode."""
        if not self.requires_credentials:
            return True
        resolved = self.credentials()
        mode = self.auth_mode

        if mode == "browser_session":
            # The captured session *is* the credential. Env username/password
            # are only needed to (re-)capture it interactively.
            path = self.session_file()
            return bool(path and path.is_file())

        if mode == "client_certificate":
            # The certificate is the credential. A form login, where the portal
            # also wants one, is layered on top of the TLS handshake.
            if self.client_certificate() is None:
                return False
            if self.auth.get("form_login_required"):
                return bool(resolved.get("username") and resolved.get("password"))
            return True

        if mode == "api_key":
            return bool(resolved.get("api_key"))
        if mode in {"session_login", "oauth2", "browser_login"}:
            has_login = bool(resolved.get("username") and resolved.get("password"))
            return has_login or bool(resolved.get("api_key"))
        return bool(resolved)

    def missing_credentials(self) -> list[str]:
        """Environment variables that still need a value.

        Surfaced to operators so "credentials_missing" says *which* ones,
        instead of leaving them to guess.
        """
        if not self.requires_credentials or self.has_credentials():
            return []

        if self.auth_mode == "browser_session":
            # Not an env var at all — name the command that fixes it.
            return [
                f"(captured session missing) run: "
                f"smarttender-admin capture-login {self.key}"
            ]

        mapping = self.auth.get("credentials_env") or {}
        resolved = self.credentials()
        return sorted(
            str(env_var)
            for logical, env_var in mapping.items()
            if not resolved.get(logical)
        )

    # ------------------------------------------------------------------
    @property
    def checksum(self) -> str:
        """Stable digest of the effective config.

        Stored on the ``Source`` row so a health regression can be correlated
        with the config edit that caused it.
        """
        payload = orjson.dumps(self.raw, option=orjson.OPT_SORT_KEYS)
        return hashlib.sha256(payload).hexdigest()

    def describe(self) -> dict[str, Any]:
        """Safe-to-log summary. Deliberately excludes anything credential-shaped."""
        return {
            "key": self.key,
            "name": self.name,
            "enabled": self.enabled,
            "strategy": self.strategy.value,
            "base_url": self.base_url,
            "requires_credentials": self.requires_credentials,
            "has_credentials": self.has_credentials(),
            "checksum": self.checksum[:12],
        }


def load_connector_config(key: str) -> ConnectorConfig:
    """Load and resolve ``config/connectors/<key>.yaml``."""
    try:
        raw = load_yaml_config(key, subdir="connectors")
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"No configuration file for connector '{key}'.",
            context={"connector": key},
            cause=exc,
        ) from exc

    declared_key = raw.get("key") or key
    if declared_key != key:
        raise ConfigurationError(
            "Connector config filename does not match its declared key.",
            context={"filename": key, "declared_key": declared_key},
        )

    for required in ("name", "strategy"):
        if not raw.get(required):
            raise ConfigurationError(
                f"Connector config is missing required field '{required}'.",
                context={"connector": key},
            )

    http_defaults = load_yaml_config("http")
    http_effective = deep_merge(http_defaults, raw.get("http") or {})

    return ConnectorConfig(
        key=key,
        name=str(raw["name"]),
        enabled=bool(raw.get("enabled", True)),
        strategy=coerce(FetchStrategy, raw.get("strategy"), FetchStrategy.STATIC),
        base_url=str(raw.get("base_url") or ""),
        raw=raw,
        http=http_effective,
    )
