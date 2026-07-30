"""Browser-captured session reuse.

The problem this solves: some sources can only be *authenticated* through a
browser (OAuth, anti-bot challenges, JS-built login forms), but once you hold a
session cookie the actual data is a cheap JSON API. Driving a browser for every
page of a crawl then costs roughly a hundred times what it needs to.

So the two are split:

    capture (rare, interactive)   Playwright logs in once, headed if needed,
                                  and exports `storage_state` — Playwright's
                                  own cookie/localStorage snapshot format.

    crawl (hot path, headless)    httpx loads those cookies and does the
                                  paginated fetching at full speed.

A captured session is a **credential**. It is stored outside the repository,
never logged, and treated as expiring — a long crawl detects a session that has
lapsed and stops rather than silently recording empty pages.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = ["BrowserSession", "load_session", "save_session", "session_path"]


@dataclass(slots=True)
class BrowserSession:
    """Cookies and headers captured from an authenticated browser context."""

    cookies: dict[str, str]
    origins: list[str]
    captured_at: datetime | None = None
    #: Extra headers worth replaying (User-Agent above all — some backends tie
    #: the session to it, and a mismatch reads as session hijacking).
    headers: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {}

    @property
    def age_hours(self) -> float | None:
        if self.captured_at is None:
            return None
        return (datetime.now(timezone.utc) - self.captured_at).total_seconds() / 3600

    def describe(self) -> dict[str, Any]:
        """Safe-to-log summary. Never includes cookie values."""
        summary: dict[str, Any] = {
            "cookie_names": sorted(self.cookies),
            "origins": self.origins,
            "age_hours": round(self.age_hours, 1) if self.age_hours is not None else None,
        }
        for name, cookie in (("access", "JWT-access"), ("refresh", "JWT-refresh")):
            expiry = self.token_expiry(cookie)
            if expiry is not None:
                summary[f"{name}_expires_in_min"] = round(
                    (expiry - datetime.now(timezone.utc)).total_seconds() / 60
                )
        return summary

    def token_expiry(self, cookie_name: str) -> datetime | None:
        """Expiry of a JWT held in a cookie, or ``None`` if it is not a JWT.

        Reads the ``exp`` claim from the payload. The signature is neither
        verified nor needed — we are not validating the token, only asking the
        server's own stated deadline so we can refresh before it passes.
        """
        token = self.cookies.get(cookie_name)
        if not token or token.count(".") != 2:
            return None
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            exp = claims.get("exp")
            return datetime.fromtimestamp(float(exp), tz=timezone.utc) if exp else None
        except Exception:
            return None

    def token_is_fresh(self, cookie_name: str, *, margin_seconds: float = 60) -> bool:
        """Whether a JWT is still valid, with a margin for the request itself.

        ``True`` for a non-JWT cookie: an opaque session has no expiry we can
        read, so it is the server's job to reject it.
        """
        expiry = self.token_expiry(cookie_name)
        if expiry is None:
            return True
        return (expiry - datetime.now(timezone.utc)).total_seconds() > margin_seconds


def session_dir() -> Path:
    """Directory holding captured sessions.

    Overridable via ``SMARTTENDER_SESSION_DIR`` so deployments can mount it as
    a secret volume — and so tests never consult the developer's real sessions,
    which would otherwise make the suite pass or fail depending on who last
    logged in.
    """
    import os

    override = os.environ.get("SMARTTENDER_SESSION_DIR")
    if override:
        return Path(override)
    from app.core.config import BACKEND_ROOT

    return BACKEND_ROOT / "certs"


def session_path(connector_key: str, configured: str | None = None) -> Path:
    """Where a connector's captured session lives."""
    if configured:
        return Path(configured)
    return session_dir() / f"{connector_key}-session.json"


def save_session(
    path: Path,
    storage_state: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> None:
    """Persist a Playwright ``storage_state`` plus the headers to replay."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "storage_state": storage_state,
        "headers": headers or {},
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        # Best effort on POSIX; a no-op on Windows. The file is a credential.
        path.chmod(0o600)
    except OSError:
        pass
    logger.info("session.saved", path=str(path))


def load_session(path: Path, *, max_age_hours: float | None = None) -> BrowserSession:
    """Load a captured session, or raise ``AuthenticationError``.

    Raising rather than returning ``None`` is deliberate: a connector that gets
    this far *needs* the session, and a missing or stale one must surface as a
    clear "re-capture your login" instruction rather than as a wall of empty
    pages an hour later.
    """
    if not path.is_file():
        raise AuthenticationError(
            "No captured browser session. Run "
            "`smarttender-admin capture-login <connector>` to create one.",
            context={"expected_path": str(path)},
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthenticationError(
            "Captured session file is unreadable; re-capture the login.",
            context={"path": str(path)},
            cause=exc,
        ) from exc

    state = payload.get("storage_state") or {}
    cookies = {
        str(c["name"]): str(c["value"])
        for c in state.get("cookies", [])
        if c.get("name") and c.get("value") is not None
    }
    if not cookies:
        raise AuthenticationError(
            "Captured session contains no cookies; re-capture the login.",
            context={"path": str(path)},
        )

    captured_at = None
    raw_time = payload.get("captured_at")
    if raw_time:
        try:
            captured_at = datetime.fromisoformat(raw_time)
            if captured_at.tzinfo is None:
                captured_at = captured_at.replace(tzinfo=timezone.utc)
        except ValueError:
            captured_at = None

    session = BrowserSession(
        cookies=cookies,
        origins=[str(o.get("origin")) for o in state.get("origins", []) if o.get("origin")],
        captured_at=captured_at,
        headers={str(k): str(v) for k, v in (payload.get("headers") or {}).items()},
    )

    if max_age_hours and session.age_hours is not None and session.age_hours > max_age_hours:
        raise AuthenticationError(
            f"Captured session is {session.age_hours:.0f} h old (limit "
            f"{max_age_hours:.0f} h); re-capture the login.",
            context={"path": str(path), **session.describe()},
        )

    logger.info("session.loaded", **session.describe())
    return session
