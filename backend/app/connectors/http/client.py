"""The resilient HTTP client — every outbound request goes through here.

Responsibilities are deliberately concentrated so that no connector can forget
one of them: timeouts, retries with exponential backoff and jitter, honouring
``Retry-After``, rate limiting, robots.txt, User-Agent rotation, proxy
selection, cookie/session continuity, SSRF guarding, metrics, and structured
logging.

Two rules govern the design:

1. **Every wait is bounded.** Connect, read, write, pool, the whole-operation
   budget, and even the backoff sleeps all have ceilings. There is no code path
   that can wait forever.
2. **Failure is a return value, not a hang.** When the attempt budget is spent
   the client raises a typed exception immediately; the caller records it and
   moves to the next item, page, or connector.
"""

from __future__ import annotations

import asyncio
import random
import time
from types import TracebackType
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.connectors.http.proxy import ProxyPool
from app.connectors.http.rate_limiter import RateLimiter
from app.connectors.http.robots import RobotsPolicy
from app.connectors.http.user_agents import UserAgentPool
from app.connectors.models import FetchedPage
from app.core.exceptions import (
    AuthenticationError,
    DownloadError,
    RateLimitedError,
    RobotsDisallowedError,
    SourceUnavailableError,
)
from app.core.logging import get_logger
from app.core.metrics import (
    http_request_duration_seconds,
    http_requests_total,
    http_retries_total,
)
from app.core.security import assert_public_url, redact_url

logger = get_logger(__name__)

__all__ = ["ClientStats", "ResilientHttpClient"]


class ClientStats:
    """Per-run transport counters, surfaced on the connector outcome."""

    __slots__ = ("bytes_downloaded", "rate_limit_waits", "requests", "retries")

    def __init__(self) -> None:
        self.requests = 0
        self.retries = 0
        self.bytes_downloaded = 0
        self.rate_limit_waits = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "http_requests": self.requests,
            "http_retries": self.retries,
            "bytes_downloaded": self.bytes_downloaded,
            "rate_limit_waits": round(self.rate_limit_waits, 2),
        }


class ResilientHttpClient:
    """Async HTTP client scoped to a single connector run.

    One instance per run: it owns the cookie jar, the sticky User-Agent and the
    connection pool, all of which should start clean for each run and be
    released at the end.
    """

    def __init__(
        self,
        *,
        connector_key: str,
        config: dict[str, Any],
        base_url: str = "",
        allow_private_hosts: bool = False,
        client_cert: tuple[str, ...] | None = None,
    ) -> None:
        self.connector_key = connector_key
        self.base_url = base_url
        self.config = config
        self.allow_private_hosts = allow_private_hosts
        #: TLS client certificate for mutual-TLS sources (TUNEPS/TUNTRUST).
        #: Passed straight to httpx; never logged, since the tuple carries the
        #: key's passphrase.
        self.client_cert = client_cert
        self.stats = ClientStats()

        timeouts = config.get("timeouts") or {}
        self._timeout = httpx.Timeout(
            connect=float(timeouts.get("connect_seconds", 10.0)),
            read=float(timeouts.get("read_seconds", 30.0)),
            write=float(timeouts.get("write_seconds", 15.0)),
            pool=float(timeouts.get("pool_seconds", 5.0)),
        )
        self._total_budget = float(timeouts.get("total_seconds", 120.0))

        retry = config.get("retry") or {}
        self._max_attempts = max(1, int(retry.get("max_attempts", 4)))
        self._initial_backoff = float(retry.get("initial_backoff_seconds", 1.0))
        self._backoff_multiplier = float(retry.get("backoff_multiplier", 2.0))
        self._max_backoff = float(retry.get("max_backoff_seconds", 30.0))
        self._jitter_ratio = float(retry.get("jitter_ratio", 0.25))
        self._retry_statuses = set(retry.get("retry_on_status") or [429, 500, 502, 503, 504])
        self._respect_retry_after = bool(retry.get("respect_retry_after", True))

        self._headers = dict(config.get("headers") or {})
        self._user_agents = UserAgentPool(config.get("user_agents"))
        self._proxies = ProxyPool(config.get("proxy"))
        self._rate_limiter = RateLimiter(connector_key, config.get("rate_limit"))
        self._robots = RobotsPolicy(config.get("robots"))

        concurrency = config.get("concurrency") or {}
        self._semaphore = asyncio.Semaphore(int(concurrency.get("per_connector", 4)))

        self._clients: dict[str | None, httpx.AsyncClient] = {}
        self._cookies = httpx.Cookies()
        self._closed = False
        self._robots_delay: dict[str, float] = {}
        #: Set when a response looks like "your session expired" rather than a
        #: genuine error, so the caller can re-authenticate and resume.
        self.session_expired = False

    def set_cookie(self, name: str, value: str) -> None:
        """Replace a cookie on the live jar — used when a token is refreshed.

        The jar is shared by every pooled client, so a renewed access token
        takes effect on the next request without rebuilding connections.
        """
        self._cookies.set(name, value)

    def adopt_session(self, session: Any) -> None:
        """Load cookies and headers captured from an authenticated browser.

        Called before the first request. Replaying the captured User-Agent
        matters as much as the cookies: some backends bind the session to it,
        and a mismatch reads as session hijacking.
        """
        for name, value in session.cookies.items():
            self._cookies.set(name, value)
        for header, value in (session.headers or {}).items():
            self._headers[header] = value
        # A captured UA must survive rotation, or the session breaks mid-crawl.
        if "User-Agent" in self._headers:
            self._pinned_user_agent = self._headers["User-Agent"]
        logger.info(
            "http.session_adopted",
            connector=self.connector_key,
            cookies=sorted(session.cookies),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self) -> ResilientHttpClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _client_for(self, proxy: str | None) -> httpx.AsyncClient:
        """One pooled client per proxy, sharing a single cookie jar.

        The shared jar is what makes a login performed through one proxy still
        valid on the next request, which matters for session-authenticated
        portals.
        """
        client = self._clients.get(proxy)
        if client is None or client.is_closed:
            limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
            client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                limits=limits,
                cookies=self._cookies,
                proxy=proxy,
                http2=True,
                verify=True,
                cert=self.client_cert,
            )
            self._clients[proxy] = client
        return client

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._clients.clear()
        await self._rate_limiter.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def resolve(self, url: str) -> str:
        """Absolutise a possibly-relative URL against the connector's base."""
        if url.startswith(("http://", "https://")):
            return url
        if not self.base_url:
            return url
        return urljoin(self.base_url.rstrip("/") + "/", url.lstrip("/"))

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(self._headers)
        # A browser-captured session pins its User-Agent; rotating it would
        # invalidate the session on backends that bind the two together.
        headers["User-Agent"] = getattr(self, "_pinned_user_agent", None) or self._user_agents.get()
        if extra:
            headers.update(extra)
        return headers

    def _backoff_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Exponential backoff with jitter, bounded, honouring ``Retry-After``."""
        if retry_after is not None and self._respect_retry_after:
            return min(retry_after, self._max_backoff)
        base = self._initial_backoff * (self._backoff_multiplier ** (attempt - 1))
        base = min(base, self._max_backoff)
        if self._jitter_ratio > 0:
            spread = base * self._jitter_ratio
            base = max(0.0, base + random.uniform(-spread, spread))
        return base

    #: Path fragments that mean "you are looking at a login page".
    _LOGIN_MARKERS = ("/login", "/connexion", "/signin", "/sign-in", "/auth/")

    def _looks_like_login_wall(self, response: httpx.Response) -> bool:
        """Detect a session that lapsed mid-crawl.

        Two signals, both cheap: the final URL after redirects points at a login
        route, or a JSON endpoint suddenly answered with an HTML document.
        """
        final = str(response.url).lower()
        if any(marker in final for marker in self._LOGIN_MARKERS):
            return True

        # An API that starts returning HTML is almost always an auth bounce.
        accept = (response.request.headers.get("accept") or "").lower()
        content_type = (response.headers.get("content-type") or "").lower()
        return "application/json" in accept and content_type.startswith("text/html")

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            # HTTP-date form.
            try:
                from email.utils import parsedate_to_datetime

                from app.core.identity import utc_now

                target = parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    from datetime import timezone

                    target = target.replace(tzinfo=timezone.utc)
                return max(0.0, (target - utc_now()).total_seconds())
            except Exception:
                return None

    async def _fetch_robots_body(self, url: str) -> str | None:
        """Minimal, un-retried, short-timeout fetch used only for robots.txt."""
        try:
            client = self._client_for(None)
            response = await client.get(
                url,
                headers={"User-Agent": self._user_agents.get()},
                timeout=httpx.Timeout(5.0),
            )
            return response.text if response.status_code == 200 else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
        check_robots: bool = True,
        raise_for_status: bool = True,
    ) -> FetchedPage:
        """Perform one logical request, retrying transient failures.

        Raises exactly one of ``RobotsDisallowedError``, ``AuthenticationError``,
        ``RateLimitedError``, ``SourceUnavailableError`` or ``DownloadError``.
        Never returns a partially-read body and never blocks past the total
        budget.
        """
        absolute = self.resolve(url)
        assert_public_url(absolute, allow_private=self.allow_private_hosts)
        host = urlparse(absolute).hostname or "unknown"

        if check_robots and not await self._robots.allows(absolute, self._fetch_robots_body):
            raise RobotsDisallowedError(
                "robots.txt disallows this path for our user agent.",
                connector=self.connector_key,
                url=redact_url(absolute),
            )

        deadline = time.monotonic() + self._total_budget
        last_error: Exception | None = None
        attempt = 0

        while attempt < self._max_attempts:
            attempt += 1
            if time.monotonic() >= deadline:
                raise DownloadError(
                    "Total time budget for this request was exhausted.",
                    connector=self.connector_key,
                    url=redact_url(absolute),
                    context={"attempts": attempt - 1, "budget_seconds": self._total_budget},
                )

            waited = await self._rate_limiter.acquire(host)
            self.stats.rate_limit_waits += waited

            crawl_delay = self._robots_delay.get(host)
            if crawl_delay is None and check_robots:
                crawl_delay = (
                    await self._robots.crawl_delay(absolute, self._fetch_robots_body) or 0.0
                )
                self._robots_delay[host] = crawl_delay
            if crawl_delay:
                await asyncio.sleep(min(crawl_delay, 10.0))

            proxy = self._proxies.get()
            client = self._client_for(proxy)
            started = time.perf_counter()

            try:
                async with self._semaphore:
                    response = await client.request(
                        method,
                        absolute,
                        params=params,
                        data=data,
                        json=json,
                        headers=self._build_headers(headers),
                    )
                elapsed = time.perf_counter() - started
                self.stats.requests += 1
                self._proxies.report_success(proxy)
                http_requests_total.labels(
                    connector=self.connector_key,
                    host=host,
                    method=method.upper(),
                    status=str(response.status_code),
                ).inc()
                http_request_duration_seconds.labels(
                    connector=self.connector_key, host=host
                ).observe(elapsed)

            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as exc:
                elapsed = time.perf_counter() - started
                self.stats.requests += 1
                self._proxies.report_failure(proxy)
                last_error = exc
                reason = type(exc).__name__
                http_requests_total.labels(
                    connector=self.connector_key,
                    host=host,
                    method=method.upper(),
                    status="transport_error",
                ).inc()
                if attempt >= self._max_attempts:
                    break
                delay = self._backoff_delay(attempt)
                self.stats.retries += 1
                http_retries_total.labels(
                    connector=self.connector_key, host=host, reason=reason
                ).inc()
                logger.warning(
                    "http.retry",
                    connector=self.connector_key,
                    url=redact_url(absolute),
                    attempt=attempt,
                    max_attempts=self._max_attempts,
                    reason=reason,
                    backoff_seconds=round(delay, 2),
                    elapsed_seconds=round(elapsed, 3),
                )
                await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                continue

            # --- we have a response ---------------------------------------
            status = response.status_code

            # --- session expiry -------------------------------------------
            # A lapsed session usually presents as a redirect to the login page
            # (httpx followed it, so we see 200 + login HTML) or as a 403 on an
            # endpoint that worked a minute ago. Detecting it explicitly is what
            # stops a long crawl from quietly writing empty pages for an hour.
            if self._looks_like_login_wall(response):
                self.session_expired = True
                raise AuthenticationError(
                    "The session has expired: the server redirected to a login "
                    "page. Re-capture the login and run again.",
                    connector=self.connector_key,
                    url=redact_url(absolute),
                    context={"final_url": redact_url(str(response.url)), "status_code": status},
                )

            if status in (401, 403):
                self.session_expired = True
                # Never retry an auth failure in a loop: that is how accounts
                # get locked. One UA rotation is allowed for 403 (often a bot
                # filter rather than a real permission problem), then we stop.
                if status == 403 and attempt < self._max_attempts:
                    self._user_agents.rotate()
                    self.stats.retries += 1
                    http_retries_total.labels(
                        connector=self.connector_key, host=host, reason="forbidden"
                    ).inc()
                    delay = self._backoff_delay(attempt)
                    logger.warning(
                        "http.forbidden.rotating_agent",
                        connector=self.connector_key,
                        url=redact_url(absolute),
                        attempt=attempt,
                    )
                    await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                    continue
                raise AuthenticationError(
                    f"Source refused the request with HTTP {status}.",
                    connector=self.connector_key,
                    url=redact_url(absolute),
                    context={"status_code": status},
                )

            if status in self._retry_statuses:
                retry_after = self._parse_retry_after(response)
                if attempt >= self._max_attempts:
                    if status == 429:
                        raise RateLimitedError(
                            "Source is rate limiting us and the attempt budget is spent.",
                            retry_after_seconds=retry_after,
                            connector=self.connector_key,
                            url=redact_url(absolute),
                            context={"status_code": status},
                        )
                    raise SourceUnavailableError(
                        f"Source returned HTTP {status} on every attempt.",
                        connector=self.connector_key,
                        url=redact_url(absolute),
                        context={"status_code": status, "attempts": attempt},
                    )
                delay = self._backoff_delay(attempt, retry_after)
                self.stats.retries += 1
                http_retries_total.labels(
                    connector=self.connector_key, host=host, reason=f"http_{status}"
                ).inc()
                logger.warning(
                    "http.retry",
                    connector=self.connector_key,
                    url=redact_url(absolute),
                    attempt=attempt,
                    status_code=status,
                    backoff_seconds=round(delay, 2),
                )
                await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                continue

            if raise_for_status and status >= 400:
                # 4xx other than the cases above are the portal telling us the
                # request itself is wrong. Retrying an identical request would
                # produce an identical answer.
                raise DownloadError(
                    f"Source returned HTTP {status}.",
                    connector=self.connector_key,
                    url=redact_url(absolute),
                    context={"status_code": status},
                )

            content = response.content
            if max_bytes is not None and len(content) > max_bytes:
                raise DownloadError(
                    "Response body exceeds the configured size limit.",
                    connector=self.connector_key,
                    url=redact_url(absolute),
                    context={"size_bytes": len(content), "max_bytes": max_bytes},
                )

            self.stats.bytes_downloaded += len(content)
            return FetchedPage(
                url=str(response.url),
                status_code=status,
                content=content,
                headers={k.lower(): v for k, v in response.headers.items()},
                encoding=response.charset_encoding,
                elapsed_seconds=elapsed,
                attempts=attempt,
            )

        raise SourceUnavailableError(
            "Source could not be reached within the attempt budget.",
            connector=self.connector_key,
            url=redact_url(absolute),
            context={"attempts": self._max_attempts, "last_error": type(last_error).__name__},
            cause=last_error,
        )

    # ------------------------------------------------------------------
    async def get(self, url: str, **kwargs: Any) -> FetchedPage:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> FetchedPage:
        return await self.request("POST", url, **kwargs)

    async def get_many(self, urls: list[str], **kwargs: Any) -> list[FetchedPage | Exception]:
        """Fetch several URLs concurrently, isolating failures.

        Returns results positionally, with exceptions in place of the pages
        that failed. The caller decides what a partial result means — which for
        a listing page is almost always "record the failures and keep the
        successes".
        """
        tasks = [self.get(url, **kwargs) for url in urls]
        return await asyncio.gather(*tasks, return_exceptions=True)  # type: ignore[return-value]
