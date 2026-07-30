"""Optional proxy support.

Proxies are never mandatory. When the pool is empty every method returns
``None`` and the client connects directly, so the whole feature costs nothing
when unused. The interface is shaped so that swapping a static list for a
residential-proxy provider later is a change to this file alone.
"""

from __future__ import annotations

import random
import threading
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import redact_url

logger = get_logger(__name__)

__all__ = ["ProxyPool"]


class ProxyPool:
    """Thread-safe proxy chooser with per-proxy failure tracking."""

    __slots__ = ("_failures", "_index", "_lock", "_max_failures", "_rotation", "_sticky", "_urls")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        settings = get_settings()

        # Environment wins: secrets belong there, not in YAML.
        urls = list(settings.proxy.urls) if settings.proxy.enabled else []
        if not urls:
            urls = [str(u) for u in (config.get("urls") or []) if str(u).strip()]

        enabled = bool(config.get("enabled", settings.proxy.enabled))
        self._urls: list[str] = urls if enabled else []
        self._rotation = str(config.get("rotation") or settings.proxy.rotation).lower()
        self._index = 0
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._max_failures = 3
        self._sticky: str | None = random.choice(self._urls) if self._urls else None

        if self._urls:
            logger.info("proxy.pool.initialised", size=len(self._urls), rotation=self._rotation)

    @property
    def enabled(self) -> bool:
        return bool(self._urls)

    def _healthy(self) -> list[str]:
        return [u for u in self._urls if self._failures.get(u, 0) < self._max_failures]

    def get(self) -> str | None:
        """Next proxy URL, or ``None`` for a direct connection."""
        if not self._urls:
            return None
        with self._lock:
            healthy = self._healthy()
            if not healthy:
                # Every proxy has tripped. Rather than fail the request, reset
                # the counters and try again — a dead pool must not become a
                # permanent outage.
                logger.warning("proxy.pool.all_unhealthy.resetting", size=len(self._urls))
                self._failures.clear()
                healthy = list(self._urls)
            if self._rotation == "sticky_per_run":
                if self._sticky not in healthy:
                    self._sticky = random.choice(healthy)
                return self._sticky
            if self._rotation == "random":
                return random.choice(healthy)
            proxy = healthy[self._index % len(healthy)]
            self._index += 1
            return proxy

    def report_failure(self, proxy: str | None) -> None:
        if not proxy:
            return
        with self._lock:
            self._failures[proxy] = self._failures.get(proxy, 0) + 1
            if self._failures[proxy] >= self._max_failures:
                logger.warning("proxy.marked_unhealthy", proxy=redact_url(proxy))

    def report_success(self, proxy: str | None) -> None:
        if proxy:
            with self._lock:
                self._failures.pop(proxy, None)
