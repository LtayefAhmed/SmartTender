"""User-Agent selection.

Rotation is a resilience measure, not a cloaking one: portals commonly reject
or throttle a UA that looks automated, and some vary their markup by browser.
The ``transparent`` strategy exists for cooperative public portals where
identifying ourselves honestly is the right thing to do — TUNEPS is public and
free, and being a good citizen there costs us nothing.
"""

from __future__ import annotations

import random
import threading
from typing import Any

__all__ = ["UserAgentPool"]

_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class UserAgentPool:
    """Thread-safe UA chooser.

    ``sticky`` — one UA for the lifetime of this pool instance, i.e. for one
    connector run. This is the default because a session that changes its
    browser identity mid-crawl is exactly what anti-bot heuristics look for.
    """

    __slots__ = ("_index", "_lock", "_pool", "_sticky", "_strategy", "_transparent")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self._strategy = str(config.get("strategy") or "sticky").lower()
        pool = [str(ua) for ua in (config.get("pool") or []) if str(ua).strip()]
        self._pool: list[str] = pool or [_FALLBACK]
        self._transparent = str(config.get("transparent_agent") or "").strip() or None
        self._index = 0
        self._lock = threading.Lock()
        self._sticky: str = random.choice(self._pool)

    def get(self) -> str:
        if self._strategy == "transparent" and self._transparent:
            return self._transparent
        if self._strategy == "fixed":
            return self._pool[0]
        if self._strategy == "sticky":
            return self._sticky
        if self._strategy == "round_robin":
            with self._lock:
                agent = self._pool[self._index % len(self._pool)]
                self._index += 1
                return agent
        return random.choice(self._pool)

    def rotate(self) -> str:
        """Force a new identity.

        Called after a 403 so that a retry has a chance of succeeding rather
        than replaying the exact request that was just refused.
        """
        with self._lock:
            if len(self._pool) > 1:
                candidates = [ua for ua in self._pool if ua != self._sticky]
                self._sticky = random.choice(candidates)
            self._index += 1
        return self._sticky

    def __len__(self) -> int:
        return len(self._pool)
