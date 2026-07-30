"""Generic JSON API connector.

Always prefer this over HTML scraping when a portal documents an API: it is
one or two orders of magnitude cheaper, the field semantics are explicit
instead of inferred, and it does not break when someone restyles the site.

Handles the three authentication shapes procurement APIs actually use — static
API key, session login, and OAuth2 client credentials — plus cursor, page and
offset pagination. All of it configured, none of it coded per source.

Credentials are read from the environment on demand and never stored on the
instance, logged, or echoed in an error payload.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode, urljoin

from app.connectors.base import BaseConnector
from app.connectors.models import DocumentRef, FetchedPage, NormalizedTender, RawRecord
from app.connectors.parsing.normalizers import (
    normalize_date,
    normalize_email,
    normalize_money,
    normalize_text,
)
from app.connectors.parsing.selectors import extract_json_path, require_json_items
from app.core.enums import ProcurementType, TenderStatus, coerce
from app.core.exceptions import (
    AuthenticationError,
    CredentialsMissingError,
    ParsingError,
)
from app.core.identity import canonicalize_url
from app.core.identity import utc_now as _utcnow
from app.schemas.filters import FilterApplication, TenderFilters

__all__ = ["JsonApiConnector"]


class JsonApiConnector(BaseConnector):
    """Config-driven client for a JSON tender API."""

    async def setup(self) -> None:
        from app.connectors.http.client import ResilientHttpClient

        self.http = ResilientHttpClient(
            connector_key=self.key,
            config=self.config.http,
            base_url=self.config.base_url,
            allow_private_hosts=self.context.allow_private_hosts,
            client_cert=self.config.client_certificate(),
        )
        self._auth_headers: dict[str, str] = {}
        self._token_expires_at: float = 0.0
        self._reauth_used = 0
        #: Refresh path proven to work, pinned after the first success.
        self._refresh_endpoint: str | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    async def authenticate(self) -> None:
        auth = self.config.auth
        mode = str(auth.get("mode") or "api_key").lower()

        # --- browser-captured session -------------------------------------
        # For sources whose *login* needs a browser (OAuth, anti-bot, JS forms)
        # but whose *data* is a plain JSON API. Playwright captures the session
        # once, interactively; the crawl then runs at httpx speed.
        if mode == "browser_session":
            from app.connectors.http.session_store import load_session, session_path

            path = session_path(self.key, auth.get("session_file"))
            session = load_session(path, max_age_hours=auth.get("session_max_age_hours"))
            assert self.http is not None
            self.http.adopt_session(session)
            self._session = session
            self._session_path = path
            self.log.info("connector.authenticated", mode="browser_session", **session.describe())

            # Short-lived access tokens (SimpleJWT defaults to 15 minutes) are
            # almost always already stale by the time a scheduled crawl runs,
            # so refresh up front rather than burning the first request on a
            # guaranteed 401.
            await self._refresh_access_token(reason="startup")
            return

        credentials = self.config.credentials()
        if not credentials:
            raise CredentialsMissingError(
                "This source requires a subscription and no credentials are configured.",
                connector=self.key,
                context={"expected_env": sorted((auth.get("credentials_env") or {}).values())},
            )

        assert self.http is not None

        if mode == "api_key" or (mode != "api_key" and "api_key" in credentials
                                 and not credentials.get("password")):
            header = str(auth.get("api_key_header") or "X-API-Key")
            self._auth_headers = {header: credentials["api_key"]}
            self.log.info("connector.authenticated", mode="api_key")
            return

        if mode == "session_login":
            payload = {
                str(auth.get("username_field") or "username"): credentials.get("username", ""),
                str(auth.get("password_field") or "password"): credentials.get("password", ""),
            }
            page = await self.http.post(
                auth.get("login_endpoint") or "/auth/login",
                json=payload,
                check_robots=False,
            )
            # The session cookie is now in the shared jar. Some APIs also
            # return a bearer token in the body; use it when present.
            try:
                body = page.json()
            except Exception:
                body = {}
            token = extract_json_path(body, "token") or extract_json_path(body, "access_token")
            if token:
                self._auth_headers = {"Authorization": f"Bearer {token}"}
                ttl = float(auth.get("token_ttl_seconds") or 3600)
                self._token_expires_at = time.monotonic() + ttl * 0.9
            self.log.info("connector.authenticated", mode="session_login")
            return

        if mode == "oauth2":
            page = await self.http.post(
                auth.get("token_endpoint") or "/auth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": credentials.get("username", ""),
                    "client_secret": credentials.get("password", ""),
                },
                check_robots=False,
            )
            body = page.json()
            token = extract_json_path(body, "access_token")
            if not token:
                raise AuthenticationError(
                    "Token endpoint returned no access_token.",
                    connector=self.key,
                )
            self._auth_headers = {"Authorization": f"Bearer {token}"}
            expires_in = float(extract_json_path(body, "expires_in") or
                               auth.get("token_ttl_seconds") or 3600)
            self._token_expires_at = time.monotonic() + expires_in * 0.9
            self.log.info("connector.authenticated", mode="oauth2")
            return

        raise AuthenticationError(
            f"Unsupported auth mode '{mode}'.", connector=self.key
        )

    def _refresh_endpoints(self, config: dict[str, Any]) -> list[str]:
        """Refresh paths to try, best guess first.

        A refresh endpoint is one of a handful of conventional paths, and which
        one a deployment uses is not something we can read off the session file.
        Rather than make the operator capture it from DevTools before a crawl
        will run at all, the configured value is tried first and the usual
        SimpleJWT paths after it. The one that works is remembered for the rest
        of the run, so the fallbacks cost a single round of 404s at most.
        """
        found = getattr(self, "_refresh_endpoint", None)
        if found:
            return [found]

        configured = config.get("endpoint") or []
        candidates = [configured] if isinstance(configured, str) else list(configured)
        candidates += [
            "/api/token/refresh/",      # SimpleJWT default
            "/api/token/renew/",        # the path J360's UI calls
            "/api/auth/token/refresh/",
        ]
        seen: set[str] = set()
        return [c for c in candidates if c and not (c in seen or seen.add(c))]

    async def _refresh_access_token(self, *, reason: str) -> bool:
        """Exchange the long-lived refresh token for a fresh access token.

        SimpleJWT issues a ~15-minute access token alongside a refresh token
        measured in months or years. Without this, every crawl longer than the
        access window dies partway through and the operator is told to
        "re-capture the login" — for a session that is perfectly valid.

        Returns ``True`` when the token was renewed. A failure here is not
        fatal on its own: the current token may still have life left, and the
        request that follows will say so far more precisely than a guess.
        """
        config = (self.config.auth.get("token_refresh") or {})
        session = getattr(self, "_session", None)
        if not config.get("enabled") or session is None:
            return False

        refresh_cookie = str(config.get("refresh_cookie") or "JWT-refresh")
        access_cookie = str(config.get("access_cookie") or "JWT-access")
        margin = float(config.get("refresh_margin_seconds") or 60)

        # Nothing to do while the current token comfortably outlives the margin.
        if reason != "unauthorized" and session.token_is_fresh(
            access_cookie, margin_seconds=margin
        ):
            return False

        if not session.token_is_fresh(refresh_cookie, margin_seconds=0):
            raise AuthenticationError(
                "The refresh token has itself expired; re-capture the login with "
                f"`smarttender-admin capture-login {self.key}`.",
                connector=self.key,
            )

        assert self.http is not None
        body_field = str(config.get("refresh_body_field") or "refresh")
        payload = {body_field: session.cookies[refresh_cookie]}
        method = str(config.get("method") or "POST").upper()
        token_path = config.get("access_response_path") or "access"

        token = None
        for endpoint in self._refresh_endpoints(config):
            # `raise_for_status=False` so a wrong guess reads as a 404 to step
            # past, not as an exception that aborts the whole probe.
            try:
                page = await self.http.request(
                    method, endpoint, json=payload,
                    check_robots=False, raise_for_status=False,
                )
                data = page.json() if page.status_code < 400 else {}
            except Exception as exc:
                self.log.warning(
                    "connector.token_refresh_failed",
                    reason=reason, endpoint=endpoint, error=str(exc)[:200],
                )
                return False

            token = extract_json_path(data, token_path)
            if token:
                # Remember the winner: subsequent refreshes go straight there
                # rather than replaying the 404s on every page of the crawl.
                self._refresh_endpoint = endpoint
                break

            self.log.info(
                "connector.token_refresh_endpoint_rejected",
                endpoint=endpoint, status=page.status_code,
            )
            if page.status_code in (400, 401, 403):
                # The path is right and the *token* was refused. Trying other
                # paths would only repeat the rejection — and on a metered or
                # lockout-prone account, repetition is the thing to avoid.
                break

        if not token:
            self.log.warning("connector.token_refresh_empty", reason=reason)
            return False

        # Update both the live jar and the in-memory session, so the next
        # freshness check sees the new expiry rather than the old one.
        session.cookies[access_cookie] = str(token)
        self.http.set_cookie(access_cookie, str(token))
        self.log.info(
            "connector.token_refreshed",
            reason=reason,
            expires_in_min=round(
                (session.token_expiry(access_cookie) - _utcnow()).total_seconds() / 60
            )
            if session.token_expiry(access_cookie)
            else None,
        )
        return True

    async def _ensure_authenticated(self) -> None:
        if self._token_expires_at and time.monotonic() >= self._token_expires_at:
            self.log.info("connector.token_refresh")
            await self.authenticate()
            return
        # Cheap check before every page: renew a JWT that is about to lapse.
        await self._refresh_access_token(reason="pre_request")

    # ------------------------------------------------------------------
    # Filter translation
    # ------------------------------------------------------------------
    def build_query(self, filters: TenderFilters) -> dict[str, Any]:
        mapping = self.config.filter_mapping
        values = self.config.filter_values
        query: dict[str, Any] = {}
        application = FilterApplication()

        def push(canonical: str, value: Any, *, enum_group: str | None = None) -> None:
            if value in (None, [], ""):
                return
            param = mapping.get(canonical)
            if not param:
                application.client_side.append(canonical)
                return
            if enum_group and enum_group in values:
                lookup = values[enum_group]
                value = (
                    [lookup.get(str(v), str(v)) for v in value]
                    if isinstance(value, list)
                    else lookup.get(str(value), str(value))
                )
            query[param] = ",".join(str(v) for v in value) if isinstance(value, list) else value
            application.server_side.append(canonical)

        push("keywords", filters.keywords)
        push("country", filters.countries)
        push("organization", filters.organizations)
        push("ministry", filters.ministries)
        push("funding_organization", filters.funding_organizations)
        push("sector", filters.sectors)
        push("procurement_category", filters.procurement_categories)
        push("location", filters.locations)
        push("cpv_codes", filters.cpv_codes)
        push("language", filters.languages)
        push("document_type", filters.document_types)
        push("source_website", filters.source_websites)
        push(
            "procurement_type",
            [t.value for t in filters.procurement_types],
            enum_group="procurement_type",
        )
        push("status", [s.value for s in filters.statuses], enum_group="status")
        push(
            "publication_date_from",
            filters.publication_date_from.isoformat() if filters.publication_date_from else None,
        )
        push(
            "publication_date_to",
            filters.publication_date_to.isoformat() if filters.publication_date_to else None,
        )
        push("deadline_from", filters.deadline_from.isoformat() if filters.deadline_from else None)
        push("deadline_to", filters.deadline_to.isoformat() if filters.deadline_to else None)
        push("budget_min", filters.budget_min)
        push("budget_max", filters.budget_max)

        self._filter_application = application
        return query

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------
    async def fetch(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        assert self.http is not None
        query = self.build_query(filters)
        pagination = self.config.pagination
        mode = str(pagination.get("mode") or "page").lower()
        page_size = pagination.get("page_size")
        if page_size and pagination.get("page_size_param"):
            query[str(pagination["page_size_param"])] = page_size

        search_path = self.endpoint("search")
        cursor: str | None = None
        page_number = int(pagination.get("start_page") or 1)
        #: `next_url` mode (Django REST Framework): the response hands us the
        #: absolute URL of the next page, so we never build one ourselves. This
        #: is the most robust option when the API offers it — it survives
        #: parameter renames and server-side page-size caps.
        next_url: str | None = None

        for _ in range(self.max_pages):
            if self.out_of_time:
                self.log.warning("connector.pagination_deadline")
                return

            await self._ensure_authenticated()

            if mode == "next_url" and next_url:
                url = next_url
            else:
                params = dict(query)
                if mode == "cursor":
                    if cursor:
                        params[str(pagination.get("cursor_param") or "cursor")] = cursor
                elif mode == "offset":
                    params[str(pagination.get("offset_param") or "offset")] = (
                        (page_number - 1) * int(page_size or 100)
                    )
                elif mode != "next_url":
                    params[str(pagination.get("page_param") or "page")] = page_number
                else:
                    # First request of a next_url crawl: build it normally.
                    params[str(pagination.get("page_param") or "page")] = page_number

                url = urljoin(self.config.base_url.rstrip("/") + "/", search_path.lstrip("/"))
                if params:
                    url = f"{url}?{urlencode(params, doseq=True)}"

            page = await self.http.get(url, headers=self._auth_headers, check_robots=False)
            self.note_page()
            yield page

            try:
                body = page.json()
            except Exception as exc:
                raise ParsingError(
                    "API returned a body that is not valid JSON.",
                    connector=self.key,
                    url=page.url,
                    cause=exc,
                ) from exc

            items = extract_json_path(body, self.config.response_mapping.get("items_path")) or []
            if not items and pagination.get("stop_on_empty_page", True):
                return

            if mode == "next_url":
                next_url = extract_json_path(
                    body, pagination.get("next_response_path") or "next"
                )
                if not next_url:
                    return
            elif mode == "cursor":
                cursor = extract_json_path(body, pagination.get("cursor_response_path"))
                if not cursor:
                    return
            else:
                page_number += 1
                if page_size and len(items) < int(page_size):
                    # A short page is the last page.
                    return

    # ------------------------------------------------------------------
    # Parse
    # ------------------------------------------------------------------
    def parse(self, page: FetchedPage) -> list[RawRecord]:
        try:
            body = page.json()
        except Exception as exc:
            raise ParsingError(
                "API returned a body that is not valid JSON.",
                connector=self.key,
                url=page.url,
                cause=exc,
            ) from exc

        mapping = self.config.response_mapping
        items = require_json_items(
            body, mapping.get("items_path"), connector=self.key, url=page.url
        )
        item_mapping: dict[str, str] = mapping.get("item") or {}

        records: list[RawRecord] = []
        for item in items:
            fields = {
                name: value
                for name, path in item_mapping.items()
                if (value := extract_json_path(item, path)) not in (None, [], "")
            }
            urls = fields.pop("documents", []) or []
            names = fields.pop("document_names", []) or []
            if isinstance(urls, str):
                urls = [urls]
            if isinstance(names, str):
                names = [names]

            records.append(
                RawRecord(
                    connector_key=self.key,
                    source_url=str(fields.get("source_url") or page.url),
                    fields=fields,
                    documents=[
                        {"url": str(url), "name": names[i] if i < len(names) else None}
                        for i, url in enumerate(urls)
                    ],
                )
            )
        return records

    # ------------------------------------------------------------------
    # Normalize
    # ------------------------------------------------------------------
    def normalize(self, record: RawRecord) -> NormalizedTender:
        parsing = self.config.parsing
        formats = list(parsing.get("date_formats") or [])
        tz = self.config.get("timezone")

        def text(name: str, max_length: int | None = None) -> str | None:
            return normalize_text(record.get(name), max_length=max_length)

        amount, currency = normalize_money(
            record.get("estimated_budget"),
            decimal_separator=str(parsing.get("decimal_separator") or "."),
            thousands_separator=str(parsing.get("thousands_separator") or ","),
            default_currency=record.get("currency") or parsing.get("default_currency"),
        )

        title = text("title", 1024)
        if not title:
            raise ParsingError(
                "API item has no usable title.", connector=self.key, url=record.source_url
            )

        strip_params = (self.config.get("dedup") or {}).get("strip_query_params") or []

        return NormalizedTender(
            connector_key=self.key,
            source_url=record.source_url,
            canonical_url=canonicalize_url(record.source_url, strip_params=strip_params),
            external_id=text("external_id", 255),
            reference=text("reference", 255),
            title=title,
            description=text("description"),
            buyer=text("buyer", 512),
            funding_organization=text("funding_organization", 512),
            contact_email=normalize_email(record.get("contact_email")),
            language=text("language") or self.config.language,
            country=text("buyer_country") or text("country") or self.config.country,
            location=text("location", 255),
            sector=text("sector", 255),
            category=text("category", 255),
            cpv_codes=record.get("cpv_codes") or [],
            procurement_type=coerce(
                ProcurementType,
                self._reverse_enum("procurement_type", record.get("procurement_type")),
                ProcurementType.UNKNOWN,
            ),
            status=coerce(
                TenderStatus,
                self._reverse_enum("status", record.get("status")),
                TenderStatus.UNKNOWN,
            ),
            publication_date=normalize_date(
                record.get("publication_date"), formats=formats, tz=tz, dayfirst=False
            ),
            deadline=normalize_date(record.get("deadline"), formats=formats, tz=tz, dayfirst=False),
            estimated_budget=amount,
            currency=currency,
            documents=[
                DocumentRef(url=doc["url"], name=doc.get("name"))
                for doc in record.documents
                if doc.get("url")
            ],
        )

    def _reverse_enum(self, group: str, value: Any) -> Any:
        if value is None:
            return None
        table = self.config.filter_values.get(group) or {}
        text_value = str(value).strip()
        for canonical, portal_value in table.items():
            if str(portal_value).lower() == text_value.lower():
                return canonical
        return text_value
