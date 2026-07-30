"""Circuit breaker, one per source.

Without it, a portal that has been down for six hours still costs us four
timed-out attempts per scheduled run, per worker — thousands of pointless
seconds spent waiting, worker slots occupied, and a log full of noise that
hides real problems.

    CLOSED     normal operation; consecutive failures are counted
    OPEN       calls refused immediately for `recovery_timeout`
    HALF_OPEN  a single probe is allowed; success closes, failure re-opens

Refusing a call raises ``CircuitOpenError``, which the connector converts into a
*skipped* outcome — the job continues with every other source untouched.

State lives in Redis so all workers agree, and is mirrored to the ``sources``
table by the caller so operators can see it and it survives a Redis flush.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.enums import CircuitState
from app.core.exceptions import CircuitOpenError
from app.core.logging import get_logger
from app.core.metrics import circuit_breaker_state, circuit_breaker_transitions_total

logger = get_logger(__name__)

__all__ = ["BreakerSnapshot", "CircuitBreaker"]

_STATE_VALUE = {
    CircuitState.CLOSED: 0.0,
    CircuitState.HALF_OPEN: 1.0,
    CircuitState.OPEN: 2.0,
}


@dataclass(slots=True)
class BreakerSnapshot:
    state: CircuitState
    consecutive_failures: int
    consecutive_successes: int
    opened_at: float | None
    retry_after_seconds: float | None


class CircuitBreaker:
    """Shared-state breaker for one connector."""

    def __init__(self, connector_key: str, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.connector_key = connector_key
        self.enabled = bool(config.get("enabled", True))
        self.failure_threshold = int(config.get("failure_threshold") or 5)
        self.recovery_timeout = float(config.get("recovery_timeout_seconds") or 600)
        self.success_threshold = int(config.get("success_threshold") or 2)
        self.half_open_max_calls = int(config.get("half_open_max_calls") or 1)

        self._redis: Any = None
        self._redis_failed = False
        # Local mirror; also the sole store when Redis is unavailable.
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at: float | None = None
        self._half_open_calls = 0

    @property
    def _key(self) -> str:
        return f"smarttender:circuit:{self.connector_key}"

    async def _get_redis(self) -> Any:
        if self._redis_failed or not self.enabled:
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
            except Exception as exc:
                self._redis_failed = True
                logger.warning(
                    "circuit_breaker.redis_unavailable.local_only",
                    connector=self.connector_key,
                    error=str(exc),
                )
                return None
        return self._redis

    async def _load(self) -> None:
        client = await self._get_redis()
        if client is None:
            return
        try:
            data = await client.hgetall(self._key)
        except Exception as exc:
            self._redis_failed = True
            logger.warning("circuit_breaker.redis_error", error=str(exc))
            return
        if not data:
            return
        self._state = CircuitState(data.get("state", CircuitState.CLOSED.value))
        self._failures = int(data.get("failures", 0))
        self._successes = int(data.get("successes", 0))
        opened = data.get("opened_at")
        self._opened_at = float(opened) if opened else None
        self._half_open_calls = int(data.get("half_open_calls", 0))

    async def _persist(self) -> None:
        client = await self._get_redis()
        if client is None:
            return
        try:
            await client.hset(
                self._key,
                mapping={
                    "state": self._state.value,
                    "failures": self._failures,
                    "successes": self._successes,
                    "opened_at": self._opened_at or "",
                    "half_open_calls": self._half_open_calls,
                },
            )
            # Expire well after the recovery window so a permanently dead
            # source does not leave a key behind forever.
            await client.expire(self._key, int(self.recovery_timeout * 4) + 3600)
        except Exception as exc:
            self._redis_failed = True
            logger.warning("circuit_breaker.persist_failed", error=str(exc))

    def _transition(self, new_state: CircuitState) -> None:
        if new_state is self._state:
            return
        logger.warning(
            "circuit_breaker.transition",
            connector=self.connector_key,
            from_state=self._state.value,
            to_state=new_state.value,
            failures=self._failures,
        )
        self._state = new_state
        circuit_breaker_transitions_total.labels(
            connector=self.connector_key, to_state=new_state.value
        ).inc()
        circuit_breaker_state.labels(connector=self.connector_key).set(_STATE_VALUE[new_state])

    # ------------------------------------------------------------------
    async def check(self) -> None:
        """Raise ``CircuitOpenError`` if calls are currently refused."""
        if not self.enabled:
            return
        await self._load()

        if self._state is CircuitState.OPEN:
            elapsed = time.time() - (self._opened_at or 0)
            if elapsed < self.recovery_timeout:
                raise CircuitOpenError(
                    "Circuit is open for this source; skipping without calling it.",
                    connector=self.connector_key,
                    context={
                        "retry_after_seconds": round(self.recovery_timeout - elapsed, 1),
                        "consecutive_failures": self._failures,
                    },
                )
            # Recovery window elapsed: allow one probe through.
            self._transition(CircuitState.HALF_OPEN)
            self._half_open_calls = 0
            self._successes = 0
            await self._persist()

        if self._state is CircuitState.HALF_OPEN:
            if self._half_open_calls >= self.half_open_max_calls:
                raise CircuitOpenError(
                    "Circuit is half-open and its probe budget is already in use.",
                    connector=self.connector_key,
                )
            self._half_open_calls += 1
            await self._persist()

    async def record_success(self) -> None:
        if not self.enabled:
            return
        if self._state is CircuitState.HALF_OPEN:
            self._successes += 1
            if self._successes >= self.success_threshold:
                self._transition(CircuitState.CLOSED)
                self._failures = 0
                self._successes = 0
                self._half_open_calls = 0
        else:
            self._failures = 0
            circuit_breaker_state.labels(connector=self.connector_key).set(0.0)
        await self._persist()

    async def record_failure(self) -> None:
        if not self.enabled:
            return
        self._failures += 1
        self._successes = 0
        if self._state is CircuitState.HALF_OPEN:
            # The probe failed — straight back to open with a fresh window.
            self._opened_at = time.time()
            self._half_open_calls = 0
            self._transition(CircuitState.OPEN)
        elif self._failures >= self.failure_threshold:
            self._opened_at = time.time()
            self._transition(CircuitState.OPEN)
        await self._persist()

    async def snapshot(self) -> BreakerSnapshot:
        await self._load()
        retry_after = None
        if self._state is CircuitState.OPEN and self._opened_at:
            retry_after = max(0.0, self.recovery_timeout - (time.time() - self._opened_at))
        return BreakerSnapshot(
            state=self._state,
            consecutive_failures=self._failures,
            consecutive_successes=self._successes,
            opened_at=self._opened_at,
            retry_after_seconds=retry_after,
        )

    async def reset(self) -> None:
        """Force the circuit closed — the operator's "I fixed it" button."""
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._successes = 0
        self._opened_at = None
        self._half_open_calls = 0
        circuit_breaker_state.labels(connector=self.connector_key).set(0.0)
        await self._persist()

    async def close_client(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
