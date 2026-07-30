"""Turn a DevTools "Copy as cURL" string into connector configuration.

Finding a private API is a fifteen-minute manual job: log in, open Network,
filter XHR, click page 2, copy the request. Turning that copied string into a
working connector should not be a second manual job — it is entirely
mechanical, so this does it.

Given the cURL, it derives the base URL, endpoint path, query parameters, the
headers worth replaying, the pagination style, and (if a sample response is
supplied) the item path and field mapping. The output is a YAML block to paste
into ``config/connectors/<key>.yaml``.

Cookies are deliberately **not** emitted into the config: a session belongs in a
captured session file, not in a file that gets committed.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

__all__ = ["ParsedCurl", "infer_item_mapping", "parse_curl", "suggest_config"]

#: Headers worth replaying on every request. Everything else is either
#: connection-specific noise (`content-length`, `host`) or a credential that
#: belongs in the session file.
_REPLAY_HEADERS = {
    "accept",
    "accept-language",
    "content-type",
    "user-agent",
    "referer",
    "origin",
    "x-requested-with",
}
_SECRET_HEADERS = {"cookie", "authorization", "x-csrftoken", "x-xsrf-token", "x-api-key"}

#: Query parameters that are pagination controls rather than search filters.
_PAGE_PARAMS = {"page", "p", "pagenum", "page_number"}
_SIZE_PARAMS = {"page_size", "per_page", "limit", "size", "pagesize", "count"}
_OFFSET_PARAMS = {"offset", "start", "from", "skip"}
_CURSOR_PARAMS = {"cursor", "after", "next", "continuation"}


@dataclass(slots=True)
class ParsedCurl:
    """The parts of a copied request that matter."""

    method: str
    url: str
    scheme: str
    host: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    cookies: dict[str, str]
    body: Any = None
    secret_headers: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}"

    def describe(self) -> dict[str, Any]:
        """Safe-to-print summary — never includes cookie or token values."""
        return {
            "method": self.method,
            "base_url": self.base_url,
            "path": self.path,
            "query_params": sorted(self.query),
            "replayable_headers": sorted(self.headers),
            "secrets_found": sorted(self.secret_headers),
            "cookie_names": sorted(self.cookies),
        }


def parse_curl(command: str) -> ParsedCurl:
    """Parse a ``Copy as cURL`` string from Chrome, Firefox or Safari.

    Tolerates the shell line-continuations and quoting each browser emits.
    """
    # Normalise continuations: bash uses `\`, PowerShell (`Copy as cURL (cmd)`)
    # uses `^`, and both wrap lines.
    cleaned = (
        command.replace("\\\n", " ")
        .replace("^\n", " ")
        .replace("`\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )
    try:
        tokens = shlex.split(cleaned)
    except ValueError as exc:
        raise ValueError(f"Could not parse the cURL string: {exc}") from exc

    if not tokens or tokens[0] != "curl":
        raise ValueError("That does not look like a cURL command (it must start with `curl`).")

    url = ""
    method = ""
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    secrets: list[str] = []
    body: Any = None

    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in ("-X", "--request"):
            method = tokens[index + 1].upper()
            index += 2
        elif token in ("-H", "--header"):
            raw = tokens[index + 1]
            name, _, value = raw.partition(":")
            name, value = name.strip(), value.strip()
            lowered = name.lower()
            if lowered == "cookie":
                cookies.update(_parse_cookie_header(value))
                secrets.append(name)
            elif lowered in _SECRET_HEADERS:
                secrets.append(name)
            elif lowered in _REPLAY_HEADERS:
                headers[name] = value
            index += 2
        elif token in ("-b", "--cookie"):
            cookies.update(_parse_cookie_header(tokens[index + 1]))
            secrets.append("Cookie")
            index += 2
        elif token in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode"):
            raw = tokens[index + 1]
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                body = raw
            index += 2
        elif token.startswith("-"):
            # Flags we do not care about (--compressed, -k, --insecure...).
            # Two-argument flags we do not know are rare; skip just the flag.
            index += 1
        else:
            if not url:
                url = token
            index += 1

    if not url:
        raise ValueError("No URL found in the cURL command.")

    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}

    return ParsedCurl(
        method=method or ("POST" if body is not None else "GET"),
        url=url,
        scheme=parts.scheme or "https",
        host=parts.netloc,
        path=parts.path or "/",
        query=query,
        headers=headers,
        cookies=cookies,
        body=body,
        secret_headers=sorted(set(secrets)),
    )


def _parse_cookie_header(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for pair in value.split(";"):
        name, _, val = pair.strip().partition("=")
        if name:
            cookies[name.strip()] = val.strip()
    return cookies


def detect_pagination(query: dict[str, str], sample: Any = None) -> dict[str, Any]:
    """Infer the pagination style from the query parameters and a response.

    A Django REST Framework envelope (``count``/``next``/``results``) is
    detected first and wins, because following the server's own ``next`` URL is
    strictly more robust than reconstructing parameters ourselves.
    """
    if isinstance(sample, dict) and "next" in sample and "results" in sample:
        return {
            "mode": "next_url",
            "next_response_path": "next",
            "page_param": _first(query, _PAGE_PARAMS) or "page",
            "page_size_param": _first(query, _SIZE_PARAMS),
            "page_size": _int_or_none(query, _SIZE_PARAMS),
            "start_page": _int_or_none(query, _PAGE_PARAMS) or 1,
            "stop_on_empty_page": True,
        }

    cursor_param = _first(query, _CURSOR_PARAMS)
    if cursor_param:
        return {"mode": "cursor", "cursor_param": cursor_param, "cursor_response_path": "next"}

    offset_param = _first(query, _OFFSET_PARAMS)
    if offset_param:
        return {
            "mode": "offset",
            "offset_param": offset_param,
            "page_size_param": _first(query, _SIZE_PARAMS) or "limit",
            "page_size": _int_or_none(query, _SIZE_PARAMS) or 100,
        }

    return {
        "mode": "page",
        "page_param": _first(query, _PAGE_PARAMS) or "page",
        "page_size_param": _first(query, _SIZE_PARAMS),
        "page_size": _int_or_none(query, _SIZE_PARAMS),
        "start_page": _int_or_none(query, _PAGE_PARAMS) or 1,
        "stop_on_empty_page": True,
    }


def _first(query: dict[str, str], candidates: set[str]) -> str | None:
    for name in query:
        if name.lower() in candidates:
            return name
    return None


def _int_or_none(query: dict[str, str], candidates: set[str]) -> int | None:
    name = _first(query, candidates)
    if not name:
        return None
    try:
        return int(query[name])
    except (TypeError, ValueError):
        return None


#: Canonical field name -> substrings that commonly denote it in an API.
_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "external_id": ("id", "uuid", "pk"),
    "reference": ("reference", "ref", "number", "numero", "code"),
    "title": ("title", "titre", "objet", "object", "subject", "name", "libelle"),
    "description": ("description", "descriptif", "body", "summary", "resume", "detail"),
    "buyer": ("buyer", "acheteur", "organisation", "organization", "entity", "client", "pouvoir"),
    "buyer_country": ("country", "pays", "nation"),
    "publication_date": ("publi", "published", "date_pub", "created"),
    "deadline": ("deadline", "closing", "cloture", "limite", "end_date", "expiry", "echeance"),
    "estimated_budget": ("amount", "value", "montant", "budget", "estimated"),
    "currency": ("currency", "devise"),
    "location": ("location", "lieu", "place", "region", "ville", "city"),
    "sector": ("sector", "secteur", "activity", "activite", "domain"),
    "procurement_type": ("procedure", "type", "nature"),
    "status": ("status", "etat", "state"),
    "funding_organization": ("funder", "financeur", "bailleur", "donor"),
    "cpv_codes": ("cpv",),
    "contact_email": ("email", "mail", "contact"),
    "source_url": ("url", "link", "permalink", "href"),
}


def infer_item_mapping(item: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Guess a canonical field mapping from one sample record.

    A starting point to correct by hand, not an oracle — but it turns a
    forty-field response into a handful of edits.
    """
    flat = _flatten(item, prefix)
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for canonical, hints in _FIELD_HINTS.items():
        best: str | None = None
        best_score = 0.0
        for path in flat:
            if path in used:
                continue
            score = _match_score(path, hints)
            if score > best_score:
                best, best_score = path, score
        if best:
            mapping[canonical] = best
            used.add(best)
    return mapping


def _match_score(path: str, hints: tuple[str, ...]) -> float:
    """How strongly a response path denotes a canonical field.

    Scoring the *whole dotted path*, not just the leaf, is what lets nested
    records map correctly: a buyer's name arrives as ``buyer.name``, whose leaf
    (`name`) matches nothing on its own while the path is unambiguous.

    Earlier hints outrank later ones, and an exact segment match outranks a
    substring, so `id` beats `buyer_id` for `external_id`.
    """
    lowered = path.lower()
    segments = [s.strip("[]") for s in lowered.split(".")]
    leaf = segments[-1]
    best = 0.0

    for rank, hint in enumerate(hints):
        # Earlier hints are the stronger signal for this field.
        weight = 1.0 - (rank / (len(hints) + 1)) * 0.4

        if leaf == hint:
            candidate = 1.0
        elif any(segment == hint for segment in segments):
            # e.g. "buyer.name" for the `buyer` field.
            candidate = 0.9
        elif leaf.startswith(hint) or leaf.endswith(hint):
            candidate = 0.7
        elif hint in leaf:
            candidate = 0.55
        elif hint in lowered:
            candidate = 0.4
        else:
            continue

        # A nested path is slightly weaker than a flat one, all else equal.
        if len(segments) > 1 and candidate < 1.0:
            candidate -= 0.05
        best = max(best, candidate * weight)

    return best


def _flatten(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Dotted paths into a nested record, bounded so a huge payload is cheap."""
    if depth > 3 or not isinstance(value, dict):
        return []
    paths: list[str] = []
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, dict):
            paths.extend(_flatten(child, path, depth + 1))
        elif isinstance(child, list):
            paths.append(f"{path}[]" if child and isinstance(child[0], (str, int)) else path)
        else:
            paths.append(path)
    return paths


def suggest_config(
    key: str, parsed: ParsedCurl, sample: Any = None, *, name: str | None = None
) -> str:
    """Render a connector YAML block from a parsed request and sample response."""
    import yaml

    items_path = _detect_items_path(sample)
    item_mapping: dict[str, str] = {}
    if items_path is not None and isinstance(sample, dict):
        records = sample.get(items_path) if items_path else sample
        if isinstance(records, list) and records and isinstance(records[0], dict):
            item_mapping = infer_item_mapping(records[0])
    elif isinstance(sample, list) and sample and isinstance(sample[0], dict):
        item_mapping = infer_item_mapping(sample[0])

    # Query parameters that are filters, not pagination controls.
    control = _PAGE_PARAMS | _SIZE_PARAMS | _OFFSET_PARAMS | _CURSOR_PARAMS
    filters = {k: v for k, v in parsed.query.items() if k.lower() not in control}

    config: dict[str, Any] = {
        "key": key,
        "name": name or f"{key} — imported from cURL",
        "enabled": True,
        "strategy": "api",
        "base_url": parsed.base_url,
        "auth": {
            "mode": "browser_session",
            "session_file": None,
            "session_max_age_hours": 72,
        },
        "endpoints": {"search": parsed.path},
        "pagination": detect_pagination(parsed.query, sample),
        "response_mapping": {
            "items_path": items_path,
            "total_path": "count" if isinstance(sample, dict) and "count" in sample else None,
            "item": item_mapping or {"title": "REPLACE_ME"},
        },
        "filter_mapping": {"keywords": None, "country": None, "deadline_from": None},
        "parsing": {
            "date_formats": ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"],
            "decimal_separator": ".",
            "thousands_separator": ",",
        },
        "required_fields": ["title", "source_url"],
        "http": {
            "headers": parsed.headers,
            "rate_limit": {"requests_per_second": 0.5, "burst": 1},
            "concurrency": {"per_connector": 1},
            "robots": {"enabled": False},
        },
    }

    header = (
        f"# Generated from a DevTools cURL capture.\n"
        f"# Observed query parameters (kept as a record of the real request):\n"
        f"#   {filters or '(none)'}\n"
    )
    if parsed.secret_headers:
        header += (
            f"# Secrets seen in the request and deliberately NOT written here:\n"
            f"#   {', '.join(parsed.secret_headers)}\n"
            f"#   They belong in a captured session — run:\n"
            f"#     smarttender-admin capture-login {key}\n"
        )
    return header + yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100)


def _detect_items_path(sample: Any) -> str | None:
    """Find the key holding the list of records."""
    if not isinstance(sample, dict):
        return None
    for candidate in ("results", "data", "items", "records", "content", "hits", "docs"):
        if isinstance(sample.get(candidate), list):
            return candidate
    # Fall back to the first list-of-objects value.
    for key, value in sample.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return key
    return None
