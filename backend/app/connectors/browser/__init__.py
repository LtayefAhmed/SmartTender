"""Headless browser rendering, used only by connectors declaring `strategy: dynamic`."""

from app.connectors.browser.playwright_client import BrowserRenderer

__all__ = ["BrowserRenderer"]
