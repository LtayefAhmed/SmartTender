"""Distributed token-bucket rate limiting.

The bucket lives in Redis because politeness is a property of the *portal*, not
of a worker process. Ten Celery workers scraping TUNEPS must together stay
under one request per second; a per-process limiter would silently multiply the
configured rate by the number of workers — the classic way a well-behaved
scraper gets an IP banned right after a scale-up.

If Redis is unavailable the limiter degrades to a local in-process bucket and
logs once. Degraded politeness is strictly better than a stalled pipeline.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["RateLimiter"]

# Atomic refill-and-consume. Returning the wait time (rather than looping in
# Python) keeps the number of round-trips at exactly one per acquisition.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil then
  tokens = burst
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(burst, tokens + elapsed * rate)

local wait = 0
if tokens >= requested then
  tokens = tokens - requested
else
  wait = (requested - tokens) / rate
  tokens = 0
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return tostring(wait)
"""


class _LocalBucket:
    """In-process fallback bucket."""

    __slots__ = ("burst", "lock", "rate", "tokens", "updated")

    def __init__(self, rate: float, burst: float) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> float:
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0
            wait = (1.0 - self.tokens) / self.rate
            self.tokens = 0.0
            return wait


class RateLimiter:
    """Per-(connector, host) token bucket, shared across processes via Redis."""

    def __init__(self, connector_key: str, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.connector_key = connector_key
        self.enabled = bool(config.get("enabled", True))
        self.rate = float(config.get("requests_per_second") or 1.0)
        self.burst = float(config.get("burst") or max(1.0, self.rate))
        jitter = config.get("jitter_seconds") or [0.0, 0.0]
        self.jitter_min = float(jitter[0])
        self.jitter_max = float(jitter[1] if len(jitter) > 1 else jitter[0])

        self._redis: Any = None
        self._script: Any = None
        self._redis_failed = False
        self._local: dict[str, _LocalBucket] = {}

    async def _get_redis(self) -> Any:
        if self._redis_failed:
            return None
        if self._redis is None:
            try:
                from redis import asyncio as aioredis

                settings = get_settings()
                self._redis = aioredis.from_url(
                    settings.redis.cache_url,
                    socket_timeout=settings.redis.socket_timeout_seconds,
                    socket_connect_timeout=settings.redis.socket_timeout_seconds,
                    decode_responses=True,
                )
                self._script = self._redis.register_script(_TOKEN_BUCKET_LUA)
            except Exception as exc:
                self._redis_failed = True
                logger.warning(
                    "rate_limiter.redis_unavailable.using_local",
                    connector=self.connector_key,
                    error=str(exc),
                )
                return None
        return self._redis

    def _local_bucket(self, host: str) -> _LocalBucket:
        bucket = self._local.get(host)
        if bucket is None:
            bucket = _LocalBucket(self.rate, self.burst)
            self._local[host] = bucket
        return bucket

    async def acquire(self, host: str) -> float:
        """Block until a token is available. Returns the seconds actually slept."""
        if not self.enabled or self.rate <= 0:
            return 0.0

        wait = 0.0
        redis_client = await self._get_redis()
        if redis_client is not None and self._script is not None:
            key = f"smarttender:ratelimit:{self.connector_key}:{host}"
            ttl = int(max(60, self.burst / self.rate * 4))
            try:
                raw = await self._script(
                    keys=[key],
                    args=[self.rate, self.burst, time.time(), 1, ttl],
                )
                wait = float(raw)
            except Exception as exc:
                self._redis_failed = True
                logger.warning(
                    "rate_limiter.redis_error.using_local",
                    connector=self.connector_key,
                    error=str(exc),
                )
                wait = await self._local_bucket(host).acquire()
        else:
            wait = await self._local_bucket(host).acquire()

        # Politeness jitter on top of the bucket: perfectly periodic requests
        # are themselves a bot signature.
        if self.jitter_max > 0:
            wait += random.uniform(self.jitter_min, self.jitter_max)

        if wait > 0:
            # Cap a single sleep so a misconfigured rate can never park a task
            # for minutes. The caller re-acquires if it still has no token.
            wait = min(wait, 30.0)
            await asyncio.sleep(wait)
        return wait

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
            self._script = None
