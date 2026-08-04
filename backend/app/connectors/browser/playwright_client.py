"""Playwright renderer for JavaScript-driven portals.

Selenium is deliberately not used: Playwright's auto-waiting removes the class
of flaky ``sleep(3)`` scrapers that Selenium encourages, it runs headless
reliably in containers, and its network interception lets us block images and
fonts — which cuts a portal listing's load time by more than half.

**Rendering is the last resort.** In order of preference a connector should use
a documented JSON API, then static HTML, and only then a browser: a rendered
page costs roughly a hundred times the CPU and memory of an httpx GET. The
strategy is a per-connector YAML setting precisely so this stays an explicit,
reviewable decision.

Playwright is an optional import. A deployment that runs no dynamic connector
never needs the browser binaries installed, and the import error only surfaces
if such a connector actually runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

from app.connectors.models import FetchedPage
from app.core.exceptions import (
    BrowserActionError,
    ConfigurationError,
    SourceUnavailableError,
)
from app.core.logging import get_logger
from app.core.security import assert_public_url, redact_url

logger = get_logger(__name__)

__all__ = ["BrowserRenderer"]


class BrowserRenderer:
    """One browser process, one context per connector run.

    The context owns cookies and storage, so a run gets a clean session without
    paying to relaunch Chromium each time.
    """

    def __init__(
        self,
        *,
        connector_key: str,
        config: dict[str, Any],
        proxy: str | None = None,
        allow_private_hosts: bool = False,
        allow_insecure_tls: bool = False,
    ) -> None:
        browser_cfg = config.get("browser") or {}
        self.connector_key = connector_key
        self.engine = str(browser_cfg.get("engine") or "chromium")
        self.headless = bool(browser_cfg.get("headless", True))
        self.navigation_timeout_ms = int(browser_cfg.get("navigation_timeout_ms") or 45000)
        self.default_timeout_ms = int(browser_cfg.get("default_timeout_ms") or 20000)
        self.viewport = dict(browser_cfg.get("viewport") or {"width": 1440, "height": 900})
        self.locale = str(browser_cfg.get("locale") or "fr-FR")
        self.timezone = str(browser_cfg.get("timezone") or "UTC")
        self.blocked_resources = set(browser_cfg.get("block_resource_types") or [])
        self.proxy = proxy
        self.allow_private_hosts = allow_private_hosts
        #: Accept an incomplete/self-signed certificate chain. A per-connector
        #: opt-in, never a default — set only for a known portal (TUNEPS serves
        #: a chain missing its intermediate) where we read a public listing.
        self.allow_insecure_tls = allow_insecure_tls
        self.user_agent: str | None = None

        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    async def __aenter__(self) -> BrowserRenderer:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def start(self) -> None:
        async with self._lock:
            if self._context is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ConfigurationError(
                    "This connector needs a headless browser, but Playwright is not "
                    "installed. Install it and run `playwright install chromium`.",
                    context={"connector": self.connector_key},
                    cause=exc,
                ) from exc

            self._playwright = await async_playwright().start()
            launcher = getattr(self._playwright, self.engine, None)
            if launcher is None:
                raise ConfigurationError(
                    f"Unknown browser engine '{self.engine}'.",
                    context={"connector": self.connector_key},
                )

            launch_kwargs: dict[str, Any] = {
                "headless": self.headless,
                # --disable-dev-shm-usage avoids Chromium crashing in
                # containers whose /dev/shm is the default 64 MB.
                "args": ["--disable-dev-shm-usage", "--no-sandbox"],
            }
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}

            self._browser = await launcher.launch(**launch_kwargs)
            context_kwargs: dict[str, Any] = {
                "viewport": self.viewport,
                "locale": self.locale,
                "timezone_id": self.timezone,
                "ignore_https_errors": self.allow_insecure_tls,
            }
            if self.user_agent:
                context_kwargs["user_agent"] = self.user_agent
            self._context = await self._browser.new_context(**context_kwargs)
            self._context.set_default_timeout(self.default_timeout_ms)
            self._context.set_default_navigation_timeout(self.navigation_timeout_ms)

            if self.blocked_resources:
                await self._context.route("**/*", self._route_filter)

            logger.info(
                "browser.started",
                connector=self.connector_key,
                engine=self.engine,
                headless=self.headless,
            )

    async def _route_filter(self, route: Any, request: Any) -> None:
        try:
            if request.resource_type in self.blocked_resources:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass

    # ------------------------------------------------------------------
    async def render(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        wait_until: str = "domcontentloaded",
        actions: list[dict[str, Any]] | None = None,
        extra_wait_ms: int = 0,
    ) -> FetchedPage:
        """Load a page and return its final HTML.

        ``wait_for_selector`` is strongly preferred over ``extra_wait_ms``: it
        waits for the thing that actually matters and returns as soon as it
        appears, instead of always paying a fixed delay.
        """
        assert_public_url(url, allow_private=self.allow_private_hosts)
        await self.start()

        import time

        started = time.perf_counter()
        page = await self._context.new_page()
        try:
            response = await page.goto(url, wait_until=wait_until)
            status = response.status if response else 0

            if wait_for_selector:
                try:
                    await page.wait_for_selector(wait_for_selector, state="attached")
                except Exception:
                    # Not fatal here: the parser's guard selector will decide
                    # whether this page is genuinely broken or simply empty.
                    logger.warning(
                        "browser.selector_wait_timeout",
                        connector=self.connector_key,
                        url=redact_url(url),
                        selector=wait_for_selector,
                    )

            for action in actions or []:
                await self._perform(page, action)

            if extra_wait_ms:
                await page.wait_for_timeout(min(extra_wait_ms, 10_000))

            html = await page.content()
            return FetchedPage(
                url=page.url,
                status_code=status or 200,
                content=html.encode("utf-8"),
                headers={},
                encoding="utf-8",
                elapsed_seconds=time.perf_counter() - started,
                rendered=True,
            )
        except Exception as exc:
            raise SourceUnavailableError(
                "Headless rendering failed for this page.",
                connector=self.connector_key,
                url=redact_url(url),
                context={"error": type(exc).__name__},
                cause=exc,
            ) from exc
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def render_paginated(
        self,
        url: str,
        *,
        rows_selector: str,
        next_button_selector: str,
        max_pages: int,
        wait_until: str = "domcontentloaded",
        settle_ms: int = 1500,
        actions: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[FetchedPage]:
        """Drive a single-page-app paginator, yielding each page's rendered HTML.

        Single-page apps paginate by mutating the DOM in place, not by changing
        the URL, so the whole run happens on one page object: load once, read
        the table, click "next", wait for the rows to actually change, read
        again. Waiting on a *content change* (the first row's text) rather than
        a fixed delay is what makes this reliable — the click returns before the
        new data has rendered.

        ``actions`` run once, after the table first appears and before anything
        is read — that is where a search form gets filled. Doing it here rather
        than filtering afterwards is the difference between the portal
        returning a hundred matches and us discarding a hundred non-matches.

        Yields one ``FetchedPage`` per page. Stops at ``max_pages``, when the
        next button is disabled or absent, or when the rows stop changing.
        """

        assert_public_url(url, allow_private=self.allow_private_hosts)
        await self.start()
        page = await self._context.new_page()

        try:
            response = await page.goto(url, wait_until=wait_until)
            status = response.status if response else 200
            try:
                await page.wait_for_selector(rows_selector, state="attached")
            except Exception as exc:
                # The table never rendered. Yield one page anyway so the parser's
                # guard selector produces the alerting SelectorBrokenError, which
                # is the signal we actually want.
                logger.warning(
                    "browser.paginated.no_rows",
                    connector=self.connector_key,
                    url=redact_url(url),
                    error=str(exc),
                )
                html = await page.content()
                yield FetchedPage(
                    url=page.url, status_code=status,
                    content=html.encode("utf-8"), encoding="utf-8", rendered=True,
                )
                return

            if actions:
                before = await self._first_row_signature(page, rows_selector)
                for action in actions:
                    await self._perform(page, action)
                # A search that returns the same first row is not proof of
                # failure — the top hit may genuinely be unchanged — so this
                # only waits, and lets the row count speak for itself.
                await self._wait_for_row_change(
                    page, rows_selector, before, timeout_ms=self.default_timeout_ms
                )
                logger.info(
                    "browser.form_applied",
                    connector=self.connector_key,
                    actions=len(actions),
                )

            for page_index in range(max_pages):
                await page.wait_for_timeout(min(settle_ms, 8000))
                first_row = await self._first_row_signature(page, rows_selector)

                html = await page.content()
                yield FetchedPage(
                    url=f"{page.url}#page={page_index + 1}",
                    status_code=status,
                    content=html.encode("utf-8"),
                    encoding="utf-8",
                    rendered=True,
                )

                nxt = await page.query_selector(next_button_selector)
                if nxt is None or not await nxt.is_enabled():
                    return

                await nxt.click()
                # Wait for the first row to change — proof the next page loaded.
                changed = await self._wait_for_row_change(
                    page, rows_selector, first_row, timeout_ms=self.default_timeout_ms
                )
                if not changed:
                    return
        except Exception as exc:
            raise SourceUnavailableError(
                "Headless pagination failed for this listing.",
                connector=self.connector_key,
                url=redact_url(url),
                context={"error": type(exc).__name__},
                cause=exc,
            ) from exc
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    async def _first_row_signature(page: Any, rows_selector: str) -> str:
        try:
            return await page.eval_on_selector(
                rows_selector, "el => el.innerText.slice(0, 120)"
            )
        except Exception:
            return ""

    async def _wait_for_row_change(
        self, page: Any, rows_selector: str, previous: str, *, timeout_ms: int
    ) -> bool:
        try:
            await page.wait_for_function(
                """([sel, prev]) => {
                    const el = document.querySelector(sel);
                    return el && el.innerText.slice(0, 120) !== prev;
                }""",
                arg=[rows_selector, previous],
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return False

    async def _perform(self, page: Any, action: dict[str, Any]) -> None:
        """Execute one declarative interaction from the connector's YAML.

        Keeping interactions declarative means a portal that adds a cookie
        banner is a config change, not a code change.
        """
        kind = str(action.get("type") or "").lower()
        selector = action.get("selector")
        try:
            if kind == "click" and selector:
                await page.click(selector, timeout=self.default_timeout_ms)
            elif kind == "fill" and selector:
                await page.fill(selector, str(action.get("value") or ""))
            elif kind == "select" and selector:
                await page.select_option(selector, str(action.get("value") or ""))
            elif kind == "material_select" and selector:
                # Angular Material renders its options into a detached overlay,
                # so `select_option` — which needs a real <select> — silently
                # does nothing. The interaction has to be a click on the
                # trigger followed by a click on the option.
                await page.click(selector, timeout=self.default_timeout_ms)
                await page.wait_for_selector("mat-option", timeout=self.default_timeout_ms)
                value = str(action.get("value") or "")
                await page.click(
                    f'mat-option:has-text("{value}")', timeout=self.default_timeout_ms
                )
            elif kind == "wait_for_selector" and selector:
                await page.wait_for_selector(selector)
            elif kind == "wait":
                await page.wait_for_timeout(min(int(action.get("ms") or 0), 10_000))
            elif kind == "scroll_to_bottom":
                await self._scroll_to_bottom(page, int(action.get("max_scrolls") or 20))
            elif kind == "press" and selector:
                await page.press(selector, str(action.get("key") or "Enter"))
        except Exception as exc:
            # An optional interaction that fails (a cookie banner that was not
            # shown) must not abort the page. A *required* one must — if
            # filling a search field fails, the portal returns everything and
            # the caller would report an unfiltered crawl as a filtered one.
            if action.get("required"):
                raise BrowserActionError(
                    f"Required browser action '{kind}' failed on {selector!r}.",
                    connector=self.connector_key,
                    context={"action": kind, "selector": str(selector)},
                    cause=exc,
                ) from exc
            logger.info(
                "browser.action_failed",
                connector=self.connector_key,
                action=kind,
                selector=selector,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    async def login(
        self,
        *,
        url: str,
        steps: list[dict[str, Any]],
        success_selector: str | None = None,
        failure_selector: str | None = None,
        settle_ms: int = 2000,
    ) -> None:
        """Sign in through the UI, leaving the session on the browser context.

        The context outlives this call, so every subsequent ``render`` reuses
        the authenticated session — one login per run rather than one per page.

        ``steps`` is the same declarative action vocabulary as ``render``, so a
        portal that adds a cookie banner or a second-factor prompt is a config
        change rather than a code change.

        Raises ``AuthenticationError`` on a failure the portal reports, and on
        a success selector that never appears — a login that silently does not
        happen is far more expensive to diagnose later than one that stops now.
        """
        from app.core.exceptions import AuthenticationError

        assert_public_url(url, allow_private=self.allow_private_hosts)
        await self.start()

        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            # Interstitial anti-bot challenges resolve themselves once the page
            # has executed its JavaScript; give them a moment before typing.
            await page.wait_for_timeout(min(settle_ms, 15_000))

            for step in steps:
                await self._perform(page, step)

            if failure_selector:
                try:
                    await page.wait_for_selector(failure_selector, timeout=3000)
                    message = await page.inner_text(failure_selector)
                    raise AuthenticationError(
                        f"Sign-in was refused: {message.strip()[:200]}",
                        connector=self.connector_key,
                    )
                except AuthenticationError:
                    raise
                except Exception:
                    pass  # the failure marker is absent, which is what we want

            if success_selector:
                try:
                    await page.wait_for_selector(success_selector)
                except Exception as exc:
                    raise AuthenticationError(
                        "Sign-in did not complete: the post-login marker never "
                        "appeared. The credentials may be wrong, or the login "
                        "selectors may be out of date.",
                        connector=self.connector_key,
                        context={"success_selector": success_selector},
                        cause=exc,
                    ) from exc

            logger.info("browser.authenticated", connector=self.connector_key)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _scroll_to_bottom(self, page: Any, max_scrolls: int) -> None:
        """Drive an infinite-scroll listing until it stops growing."""
        previous = 0
        for _ in range(max(1, min(max_scrolls, 100))):
            await page.mouse.wheel(0, 20_000)
            await page.wait_for_timeout(600)
            height = await page.evaluate("document.body.scrollHeight")
            if height == previous:
                return
            previous = height

    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._context = self._browser = self._playwright = None
