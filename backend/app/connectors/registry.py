"""Connector discovery and instantiation.

Adding a source is: drop ``config/connectors/<key>.yaml`` next to a package
``app/connectors/<key>/connector.py`` containing a ``@register`` decorated
class. Nothing else in the codebase changes — no import list to update, no
factory to extend, no ``if key == ...`` anywhere.

Discovery is fault-tolerant on purpose. A connector module with a syntax error
is recorded as unavailable and every *other* connector still loads, because a
registry that refuses to import is a registry that takes the whole platform
down over one bad source.
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.connectors.base import BaseConnector, ConnectorContext
from app.connectors.config import ConnectorConfig, load_connector_config
from app.core.config import get_settings, list_yaml_configs
from app.core.enums import SourceHealth
from app.core.exceptions import ConfigurationError
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "ConnectorInfo",
    "ConnectorRegistry",
    "connector_class",
    "get_registry",
    "register",
]


T = TypeVar("T", bound=type[BaseConnector])

#: Populated at import time by the decorator; consumed by the registry.
_REGISTERED: dict[str, type[BaseConnector]] = {}

#: Sub-packages of ``app.connectors`` that provide machinery rather than sources.
_INFRASTRUCTURE_PACKAGES = frozenset({"http", "browser", "parsing", "generic"})


def register(key: str) -> Callable[[T], T]:
    """Class decorator binding a connector implementation to a config key."""

    def decorator(cls: T) -> T:
        if key in _REGISTERED and _REGISTERED[key] is not cls:
            logger.warning("connector.registration_overridden", key=key)
        _REGISTERED[key] = cls
        cls.registry_key = key  # type: ignore[attr-defined]
        return cls

    return decorator


def connector_class(key: str) -> type[BaseConnector] | None:
    """The implementation bound to a config key, or ``None``.

    Exposed so callers outside a run — the enrichment pass, health checks —
    can ask what a connector is capable of without instantiating it, and
    without reaching into the registry's private table.
    """
    get_registry().load()
    return _REGISTERED.get(key)


@dataclass(slots=True)
class ConnectorInfo:
    """What the registry knows about one source, without instantiating it."""

    key: str
    name: str
    enabled: bool
    available: bool
    strategy: str
    base_url: str
    country: str | None
    requires_credentials: bool
    has_credentials: bool
    health: SourceHealth
    unavailable_reason: str | None = None
    checksum: str | None = None
    #: Environment variables that still need a value. Naming them turns
    #: "credentials_missing" from a riddle into an instruction.
    missing_credentials: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "enabled": self.enabled,
            "available": self.available,
            "strategy": self.strategy,
            "base_url": self.base_url,
            "country": self.country,
            "requires_credentials": self.requires_credentials,
            "has_credentials": self.has_credentials,
            "health": self.health.value,
            "unavailable_reason": self.unavailable_reason,
            "missing_credentials": self.missing_credentials,
            "config_checksum": self.checksum,
        }


class ConnectorRegistry:
    """Discovers, describes and instantiates connectors."""

    def __init__(self) -> None:
        self._configs: dict[str, ConnectorConfig] = {}
        self._errors: dict[str, str] = {}
        self._loaded = False
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def load(self, *, force: bool = False) -> None:
        """Import connector packages and load their configuration."""
        with self._lock:
            if self._loaded and not force:
                return
            self._configs.clear()
            self._errors.clear()
            self._discover_modules()

            for key in list_yaml_configs("connectors"):
                try:
                    config = load_connector_config(key)
                except ConfigurationError as exc:
                    self._errors[key] = exc.message
                    logger.error("connector.config_invalid", key=key, error=exc.message)
                    continue

                if key not in _REGISTERED:
                    self._errors[key] = (
                        "No implementation registered for this configuration file."
                    )
                    logger.error("connector.implementation_missing", key=key)
                    continue

                self._configs[key] = config

            self._loaded = True
            logger.info(
                "connector.registry_loaded",
                available=sorted(self._configs),
                failed=sorted(self._errors),
            )

    def _discover_modules(self) -> None:
        """Import every ``app.connectors.<pkg>.connector`` module."""
        import app.connectors as package

        for module_info in pkgutil.iter_modules(package.__path__):
            if not module_info.ispkg or module_info.name in _INFRASTRUCTURE_PACKAGES:
                continue
            module_name = f"{package.__name__}.{module_info.name}.connector"
            try:
                importlib.import_module(module_name)
            except ModuleNotFoundError as exc:
                # The package itself has no ``connector`` module — it is a
                # helper package, not a source. Anything *else* that is missing
                # is a genuine broken import and must be reported.
                if exc.name == module_name:
                    continue
                self._errors[module_info.name] = f"Import failed: {exc}"
                logger.error(
                    "connector.import_failed",
                    module=module_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            except Exception as exc:
                # One broken connector module must not prevent the others from
                # loading. Record it and move on.
                self._errors[module_info.name] = f"Import failed: {exc}"
                logger.error(
                    "connector.import_failed",
                    module=module_name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    # ------------------------------------------------------------------
    def keys(self) -> list[str]:
        self.load()
        return sorted(self._configs)

    def config(self, key: str) -> ConnectorConfig:
        self.load()
        config = self._configs.get(key)
        if config is None:
            raise ConfigurationError(
                f"Unknown connector '{key}'.",
                context={"known": sorted(self._configs), "errors": self._errors},
            )
        return config

    def errors(self) -> dict[str, str]:
        self.load()
        return dict(self._errors)

    # ------------------------------------------------------------------
    def describe(self, key: str) -> ConnectorInfo:
        config = self.config(key)
        settings = get_settings()

        available = True
        reason: str | None = None
        health = SourceHealth.UNKNOWN

        if not config.enabled:
            available, reason, health = False, "disabled", SourceHealth.DISABLED
        elif config.environments and settings.env not in config.environments:
            available, reason = False, f"not_enabled_in_env:{settings.env}"
            health = SourceHealth.DISABLED
        elif config.requires_credentials and not config.has_credentials():
            available, reason = False, "credentials_missing"
            health = SourceHealth.CREDENTIALS_MISSING
        else:
            # Preconditions only the connector class can evaluate. Asking here
            # keeps a source that cannot possibly run out of the picker, rather
            # than letting an operator select it and collect a failure.
            connector_cls = _REGISTERED.get(key)
            unmet = connector_cls.unmet_precondition(config) if connector_cls else None
            if unmet:
                available, reason, health = False, unmet, SourceHealth.DISABLED

        return ConnectorInfo(
            key=key,
            name=config.name,
            enabled=config.enabled,
            available=available,
            strategy=config.strategy.value,
            base_url=config.base_url,
            country=config.country,
            requires_credentials=config.requires_credentials,
            has_credentials=config.has_credentials(),
            health=health,
            unavailable_reason=reason,
            checksum=config.checksum,
            missing_credentials=config.missing_credentials(),
        )

    def describe_all(self) -> list[ConnectorInfo]:
        return [self.describe(key) for key in self.keys()]

    def available_keys(self) -> list[str]:
        """Connectors that would actually run right now.

        This is what an empty ``connectors`` list in a job resolves to, and it
        is why a scheduled run against "all sources" quietly excludes J360 when
        no subscription is configured instead of failing.
        """
        return [info.key for info in self.describe_all() if info.available]

    # ------------------------------------------------------------------
    def create(self, key: str, context: ConnectorContext | None = None) -> BaseConnector:
        """Instantiate a connector. One instance per run — never shared."""
        config = self.config(key)
        implementation = _REGISTERED.get(key)
        if implementation is None:
            raise ConfigurationError(
                f"No implementation registered for connector '{key}'.",
                context={"connector": key},
            )
        return implementation(config, context)

    def resolve_requested(self, requested: list[str] | None) -> tuple[list[str], list[str]]:
        """Turn a requested connector list into (runnable, skipped).

        An unknown key is *skipped*, not fatal: a stored schedule that names a
        connector someone later removed should keep running the rest.
        """
        self.load()
        if not requested:
            return self.available_keys(), []

        runnable: list[str] = []
        skipped: list[str] = []
        for key in requested:
            if key not in self._configs:
                skipped.append(key)
                logger.warning("connector.requested_but_unknown", key=key)
                continue
            info = self.describe(key)
            (runnable if info.available else skipped).append(key)
        return runnable, skipped

    def reset(self) -> None:
        with self._lock:
            self._configs.clear()
            self._errors.clear()
            self._loaded = False


_registry: ConnectorRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ConnectorRegistry:
    """Process-wide registry singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ConnectorRegistry()
    return _registry
