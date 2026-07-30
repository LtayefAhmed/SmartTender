"""HTTP transport: retries, backoff, timeouts, rate limiting and the breaker.

Every request is mocked with respx, so the suite exercises the real retry and
backoff logic without a single network call and without a single real sleep of
consequence.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.connectors.http.circuit_breaker import CircuitBreaker
from app.connectors.http.client import ResilientHttpClient
from app.connectors.http.rate_limiter import RateLimiter
from app.connectors.http.robots import RobotsPolicy
from app.connectors.http.user_agents import UserAgentPool
from app.core.enums import CircuitState
from app.core.exceptions import (
    AuthenticationError,
    CircuitOpenError,
    DownloadError,
    RateLimitedError,
    RobotsDisallowedError,
    SourceUnavailableError,
)

BASE = "https://portal.example.tn"


def _config(**overrides):
    config = {
        "timeouts": {
            "connect_seconds": 1.0,
            "read_seconds": 2.0,
            "write_seconds": 1.0,
            "pool_seconds": 1.0,
            "total_seconds": 20.0,
        },
        "retry": {
            "max_attempts": 4,
            "initial_backoff_seconds": 0.01,
            "backoff_multiplier": 2.0,
            "max_backoff_seconds": 0.05,
            "jitter_ratio": 0.0,
            "retry_on_status": [429, 500, 502, 503, 504],
            "respect_retry_after": True,
        },
        "rate_limit": {"enabled": False},
        "robots": {"enabled": False},
        "headers": {"Accept": "text/html"},
        "user_agents": {"strategy": "sticky", "pool": ["UA-1", "UA-2", "UA-3"]},
        "concurrency": {"per_connector": 4},
        "proxy": {"enabled": False},
    }
    config.update(overrides)
    return config


def _client(**overrides) -> ResilientHttpClient:
    return ResilientHttpClient(
        connector_key="probe", config=_config(**overrides), base_url=BASE
    )


class TestSuccessPath:
    @respx.mock
    def test_a_successful_get(self):
        respx.get(f"{BASE}/list").mock(return_value=httpx.Response(200, text="<html>ok</html>"))

        async def run():
            async with _client() as client:
                return await client.get("/list")

        page = asyncio.run(run())
        assert page.status_code == 200
        assert "ok" in page.text
        assert page.attempts == 1

    @respx.mock
    def test_relative_urls_resolve_against_the_base(self):
        route = respx.get(f"{BASE}/fr/avis").mock(return_value=httpx.Response(200))

        async def run():
            async with _client() as client:
                await client.get("/fr/avis")

        asyncio.run(run())
        assert route.called

    @respx.mock
    def test_transport_statistics_are_recorded(self):
        respx.get(f"{BASE}/a").mock(return_value=httpx.Response(200, text="a"))
        respx.get(f"{BASE}/b").mock(return_value=httpx.Response(200, text="bb"))

        async def run():
            async with _client() as client:
                await client.get("/a")
                await client.get("/b")
                return client.stats

        stats = asyncio.run(run())
        assert stats.requests == 2
        assert stats.bytes_downloaded == 3


class TestRetryAndBackoff:
    @respx.mock
    def test_a_transient_500_is_retried_then_succeeds(self):
        route = respx.get(f"{BASE}/flaky")
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, text="recovered"),
        ]

        async def run():
            async with _client() as client:
                return await client.get("/flaky"), client.stats

        page, stats = asyncio.run(run())
        assert page.text == "recovered"
        assert page.attempts == 3
        assert stats.retries == 2

    @respx.mock
    def test_the_attempt_budget_is_finite(self):
        respx.get(f"{BASE}/dead").mock(return_value=httpx.Response(503))

        async def run():
            async with _client() as client:
                await client.get("/dead")

        with pytest.raises(SourceUnavailableError) as excinfo:
            asyncio.run(run())
        assert excinfo.value.context["attempts"] == 4
        assert excinfo.value.retryable is True

    @respx.mock
    def test_a_network_error_is_retried(self):
        route = respx.get(f"{BASE}/net")
        route.side_effect = [
            httpx.ConnectError("refused"),
            httpx.Response(200, text="ok"),
        ]

        async def run():
            async with _client() as client:
                return await client.get("/net")

        assert asyncio.run(run()).text == "ok"

    @respx.mock
    def test_a_timeout_is_retried_then_reported(self):
        respx.get(f"{BASE}/slow").mock(side_effect=httpx.ReadTimeout("too slow"))

        async def run():
            async with _client() as client:
                await client.get("/slow")

        with pytest.raises(SourceUnavailableError):
            asyncio.run(run())

    def test_backoff_grows_exponentially(self):
        client = _client()
        delays = [client._backoff_delay(attempt) for attempt in range(1, 4)]
        assert delays[0] < delays[1] <= delays[2]

    def test_backoff_is_capped(self):
        client = _client()
        assert client._backoff_delay(20) <= 0.05

    def test_jitter_spreads_simultaneous_retries(self):
        """Five hundred tasks failing together must not retry in lockstep."""
        client = _client(
            retry={
                "max_attempts": 4,
                "initial_backoff_seconds": 1.0,
                "backoff_multiplier": 2.0,
                "max_backoff_seconds": 30.0,
                "jitter_ratio": 0.25,
            }
        )
        samples = {round(client._backoff_delay(3), 6) for _ in range(30)}
        assert len(samples) > 5

    @respx.mock
    def test_retry_after_is_honoured(self):
        route = respx.get(f"{BASE}/limited")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0.02"}),
            httpx.Response(200, text="ok"),
        ]

        async def run():
            async with _client() as client:
                return await client.get("/limited")

        assert asyncio.run(run()).text == "ok"

    @respx.mock
    def test_persistent_429_raises_rate_limited(self):
        respx.get(f"{BASE}/limited").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "0.01"})
        )

        async def run():
            async with _client() as client:
                await client.get("/limited")

        with pytest.raises(RateLimitedError):
            asyncio.run(run())


class TestNonRetryablePaths:
    @respx.mock
    def test_401_is_never_retried(self):
        route = respx.get(f"{BASE}/private").mock(return_value=httpx.Response(401))

        async def run():
            async with _client() as client:
                await client.get("/private")

        with pytest.raises(AuthenticationError):
            asyncio.run(run())
        # Hammering a login endpoint gets the account locked.
        assert route.call_count == 1

    @respx.mock
    def test_403_rotates_the_user_agent_once_then_stops(self):
        route = respx.get(f"{BASE}/blocked").mock(return_value=httpx.Response(403))

        async def run():
            async with _client() as client:
                await client.get("/blocked")

        with pytest.raises(AuthenticationError):
            asyncio.run(run())
        assert route.call_count == 4    # rotations, then a definitive stop

    @respx.mock
    def test_404_is_not_retried(self):
        route = respx.get(f"{BASE}/missing").mock(return_value=httpx.Response(404))

        async def run():
            async with _client() as client:
                await client.get("/missing")

        with pytest.raises(DownloadError):
            asyncio.run(run())
        assert route.call_count == 1


class TestGuards:
    @respx.mock
    def test_an_oversized_body_is_refused(self):
        respx.get(f"{BASE}/big").mock(return_value=httpx.Response(200, content=b"x" * 5000))

        async def run():
            async with _client() as client:
                await client.get("/big", max_bytes=1000)

        with pytest.raises(DownloadError) as excinfo:
            asyncio.run(run())
        assert excinfo.value.context["max_bytes"] == 1000

    def test_internal_hosts_are_refused(self):
        from app.core.exceptions import ValidationError

        async def run():
            async with _client() as client:
                await client.get("http://169.254.169.254/latest/meta-data/")

        with pytest.raises(ValidationError):
            asyncio.run(run())

    @respx.mock
    def test_concurrent_fetches_isolate_failures(self):
        respx.get(f"{BASE}/ok").mock(return_value=httpx.Response(200, text="fine"))
        respx.get(f"{BASE}/bad").mock(return_value=httpx.Response(404))

        async def run():
            async with _client() as client:
                return await client.get_many([f"{BASE}/ok", f"{BASE}/bad", f"{BASE}/ok"])

        results = asyncio.run(run())
        assert results[0].status_code == 200
        assert isinstance(results[1], DownloadError)
        assert results[2].status_code == 200


class TestUserAgentPool:
    def test_sticky_keeps_one_identity_per_run(self):
        pool = UserAgentPool({"strategy": "sticky", "pool": ["A", "B", "C"]})
        assert len({pool.get() for _ in range(10)}) == 1

    def test_round_robin_cycles(self):
        pool = UserAgentPool({"strategy": "round_robin", "pool": ["A", "B"]})
        assert [pool.get() for _ in range(4)] == ["A", "B", "A", "B"]

    def test_rotate_changes_the_sticky_identity(self):
        pool = UserAgentPool({"strategy": "sticky", "pool": ["A", "B", "C"]})
        before = pool.get()
        pool.rotate()
        assert pool.get() != before

    def test_an_empty_pool_still_yields_an_agent(self):
        assert UserAgentPool({"pool": []}).get()

    def test_transparent_identifies_us_honestly(self):
        pool = UserAgentPool(
            {"strategy": "transparent", "pool": ["A"], "transparent_agent": "SmartTenderBot/1.0"}
        )
        assert pool.get() == "SmartTenderBot/1.0"


class TestRateLimiter:
    def test_a_disabled_limiter_never_waits(self):
        limiter = RateLimiter("probe", {"enabled": False})
        assert asyncio.run(limiter.acquire("host")) == 0.0

    def test_the_local_bucket_throttles_once_the_burst_is_spent(self):
        limiter = RateLimiter(
            "probe", {"enabled": True, "requests_per_second": 100.0, "burst": 2}
        )
        limiter._redis_failed = True   # force the in-process fallback

        async def run():
            waits = [await limiter.acquire("host") for _ in range(4)]
            return waits

        waits = asyncio.run(run())
        assert waits[0] == 0.0
        assert waits[1] == 0.0
        assert waits[2] > 0.0     # burst exhausted, now paced


class TestCircuitBreaker:
    def _breaker(self) -> CircuitBreaker:
        breaker = CircuitBreaker(
            "probe",
            {
                "enabled": True,
                "failure_threshold": 3,
                "recovery_timeout_seconds": 60,
                "success_threshold": 1,
            },
        )
        breaker._redis_failed = True   # local-only state for the test
        return breaker

    def test_it_opens_after_the_threshold(self):
        breaker = self._breaker()

        async def run():
            for _ in range(3):
                await breaker.record_failure()
            await breaker.check()

        with pytest.raises(CircuitOpenError):
            asyncio.run(run())

    def test_it_stays_closed_below_the_threshold(self):
        breaker = self._breaker()

        async def run():
            await breaker.record_failure()
            await breaker.record_failure()
            await breaker.check()      # must not raise
            return (await breaker.snapshot()).state

        assert asyncio.run(run()) is CircuitState.CLOSED

    def test_a_success_resets_the_failure_count(self):
        breaker = self._breaker()

        async def run():
            await breaker.record_failure()
            await breaker.record_failure()
            await breaker.record_success()
            return (await breaker.snapshot()).consecutive_failures

        assert asyncio.run(run()) == 0

    def test_reset_reopens_the_source_immediately(self):
        breaker = self._breaker()

        async def run():
            for _ in range(3):
                await breaker.record_failure()
            await breaker.reset()
            await breaker.check()      # must not raise
            return (await breaker.snapshot()).state

        assert asyncio.run(run()) is CircuitState.CLOSED


class TestRobots:
    def test_disabled_policy_allows_everything(self):
        policy = RobotsPolicy({"enabled": False})

        async def fetch(_url):
            return "User-agent: *\nDisallow: /"

        assert asyncio.run(policy.allows("https://x.tn/private", fetch)) is True

    def test_a_disallowed_path_is_refused(self):
        policy = RobotsPolicy({"enabled": True, "user_agent_token": "SmartTenderBot"})

        async def fetch(_url):
            return "User-agent: *\nDisallow: /private"

        assert asyncio.run(policy.allows("https://x.tn/private/page", fetch)) is False
        assert asyncio.run(policy.allows("https://x.tn/public/page", fetch)) is True

    def test_an_unreachable_robots_file_does_not_block_the_run(self):
        # A portal whose robots.txt is 500-ing is not telling us to stop.
        policy = RobotsPolicy({"enabled": True, "allow_on_fetch_failure": True})

        async def fetch(_url):
            raise httpx.ConnectError("no robots")

        assert asyncio.run(policy.allows("https://x.tn/page", fetch)) is True

    @respx.mock
    def test_the_client_refuses_a_disallowed_path(self):
        respx.get("https://portal.example.tn/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /secret")
        )

        async def run():
            async with _client(robots={"enabled": True, "user_agent_token": "*"}) as client:
                await client.get("/secret/page")

        with pytest.raises(RobotsDisallowedError):
            asyncio.run(run())
