"""robots.txt compliance.

Enabled per connector. Public portals we crawl politely (TUNEPS) honour it;
authenticated APIs we pay for (J360) legitimately opt out, since robots.txt
governs crawlers, not API clients acting on a subscriber's behalf.

Fetching robots.txt must never be able to block a run: it has its own short
timeout, its own cache, and a configurable failure policy that defaults to
"proceed". A portal whose robots.txt is 500-ing is not telling us to stop.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["RobotsPolicy"]


class RobotsPolicy:
    """Per-host robots.txt cache and check."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.enabled = bool(config.get("enabled", True))
        self.cache_ttl = float(config.get("cache_ttl_seconds") or 3600)
        self.user_agent_token = str(config.get("user_agent_token") or "*")
        self.allow_on_fetch_failure = bool(config.get("allow_on_fetch_failure", True))
        #: host -> (parser | None, fetched_at). ``None`` means "could not fetch".
        self._cache: dict[str, tuple[RobotFileParser | None, float]] = {}

    async def _load(self, base: str, fetch: Any) -> RobotFileParser | None:
        parsed = urlparse(base)
        host_key = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._cache.get(host_key)
        if cached and (time.monotonic() - cached[1]) < self.cache_ttl:
            return cached[0]

        robots_url = urljoin(host_key + "/", "robots.txt")
        parser: RobotFileParser | None = None
        try:
            body = await fetch(robots_url)
            if body:
                parser = RobotFileParser()
                parser.parse(body.splitlines())
        except Exception as exc:
            logger.info("robots.fetch_failed", url=robots_url, error=str(exc))
            parser = None

        self._cache[host_key] = (parser, time.monotonic())
        return parser

    async def allows(self, url: str, fetch: Any) -> bool:
        """Return whether ``url`` may be fetched.

        ``fetch`` is an ``async (url) -> str | None`` callable supplied by the
        HTTP client, which keeps this module free of any transport dependency
        and trivially testable.
        """
        if not self.enabled:
            return True
        parser = await self._load(url, fetch)
        if parser is None:
            return self.allow_on_fetch_failure
        try:
            return bool(parser.can_fetch(self.user_agent_token, url))
        except Exception:
            return self.allow_on_fetch_failure

    async def crawl_delay(self, url: str, fetch: Any) -> float | None:
        """Portal-declared crawl delay, if any.

        Honoured *in addition* to our configured rate limit: whichever is
        slower wins.
        """
        if not self.enabled:
            return None
        parser = await self._load(url, fetch)
        if parser is None:
            return None
        try:
            delay = parser.crawl_delay(self.user_agent_token)
            return float(delay) if delay is not None else None
        except Exception:
            return None

    def clear(self) -> None:
        self._cache.clear()
