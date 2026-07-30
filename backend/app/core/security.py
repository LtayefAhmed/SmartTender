"""Security primitives shared by the upload, storage and API layers.

Everything here assumes the input is hostile: filenames come from users and
from scraped portals, object keys are derived from those filenames, and URLs
come from third-party HTML.
"""

from __future__ import annotations

import hmac
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from app.core.exceptions import ValidationError
from app.core.logging import REDACTED, is_sensitive_key

__all__ = [
    "assert_public_url",
    "ensure_within",
    "is_private_host",
    "redact",
    "redact_url",
    "safe_object_key",
    "sanitize_filename",
    "verify_api_key",
]

#: Windows reserved device names — a file called ``CON.pdf`` breaks tooling on
#: any Windows host that later handles the archive.
_WINDOWS_RESERVED = frozenset(
    {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_DOT_RUNS = re.compile(r"\.{2,}")
_MAX_STEM = 120


def sanitize_filename(filename: str | None, *, default: str = "document") -> str:
    """Reduce an arbitrary filename to a safe, portable basename.

    Strips directory components (defeating ``../`` and ``..\\`` traversal and
    absolute paths), transliterates accents, collapses everything outside
    ``[A-Za-z0-9._-]``, neutralises Windows device names, and bounds the length.
    The result never starts with a dot and always has a non-empty stem.
    """
    if not filename:
        return default

    # Take the basename under BOTH separator conventions: a POSIX server must
    # still defeat "..\\..\\windows\\system32\\x" supplied by a Windows client.
    candidate = filename.replace("\\", "/").split("/")[-1].strip()
    if not candidate:
        return default

    # Decompose accents to ASCII rather than dropping the characters entirely,
    # so "Cahier_des_charges_marché.pdf" stays readable.
    candidate = unicodedata.normalize("NFKD", candidate)
    candidate = candidate.encode("ascii", "ignore").decode("ascii")

    candidate = _UNSAFE_CHARS.sub("_", candidate)
    candidate = _DOT_RUNS.sub(".", candidate).strip("._-")
    if not candidate:
        return default

    path = PurePosixPath(candidate)
    stem, suffix = path.stem, path.suffix.lower()
    if not stem:
        stem = default
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"{stem}_file"
    if len(stem) > _MAX_STEM:
        stem = stem[:_MAX_STEM]
    if len(suffix) > 16:
        suffix = suffix[:16]

    return f"{stem}{suffix}"


def safe_object_key(*parts: str) -> str:
    """Join object-storage path segments, refusing traversal and absolutes.

    Object stores treat keys as opaque strings, so ``a/../../b`` is *stored*
    literally — but anything that later mirrors the bucket onto a filesystem
    would escape the root. We reject those keys at the source.
    """
    cleaned: list[str] = []
    for raw in parts:
        if raw is None:
            continue
        segment = str(raw).strip().strip("/")
        if not segment:
            continue
        if segment.startswith("/") or ".." in segment.split("/"):
            raise ValidationError(
                "Rejected object key: path traversal attempt.",
                field="object_key",
                context={"segment": segment},
            )
        cleaned.append(segment)
    key = "/".join(cleaned)
    if not key:
        raise ValidationError("Rejected object key: empty.", field="object_key")
    if len(key) > 1024:
        raise ValidationError("Rejected object key: too long.", field="object_key")
    return key


def ensure_within(root: Any, candidate: Any) -> Any:
    """Assert ``candidate`` resolves inside ``root``; return the resolved path."""
    from pathlib import Path

    root_resolved = Path(root).resolve()
    target = Path(candidate)
    target_resolved = (
        target.resolve() if target.is_absolute() else (root_resolved / target).resolve()
    )
    if root_resolved != target_resolved and root_resolved not in target_resolved.parents:
        raise ValidationError(
            "Rejected path: escapes the permitted root.",
            field="path",
            context={"root": str(root_resolved), "path": str(target_resolved)},
        )
    return target_resolved


def redact(payload: Any, key: str = "") -> Any:
    """Recursively replace sensitive values. Use before logging any dict that
    may have come from a connector config or an HTTP exchange.

    Containers are recursed into rather than masked wholesale — see
    :func:`app.core.logging._redact` for why.
    """
    if isinstance(payload, dict):
        return {k: redact(v, str(k)) for k, v in payload.items()}
    if isinstance(payload, (list, tuple, set)):
        return type(payload)(redact(v, key) for v in payload)
    if is_sensitive_key(key):
        return REDACTED
    return payload


def redact_url(url: str | None) -> str | None:
    """Strip userinfo and obvious secret query parameters from a URL."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:  # pragma: no cover - urlparse is very permissive
        return REDACTED
    netloc = parsed.hostname or ""
    if parsed.username:
        netloc = f"{REDACTED}@{netloc}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    query = parsed.query
    if query:
        pairs = []
        for pair in query.split("&"):
            name, _, value = pair.partition("=")
            if is_sensitive_key(name):
                pairs.append(f"{name}={REDACTED}")
            else:
                pairs.append(f"{name}={value}" if value else name)
        query = "&".join(pairs)
    rebuilt = f"{parsed.scheme}://{netloc}{parsed.path}"
    return f"{rebuilt}?{query}" if query else rebuilt


def verify_api_key(presented: str | None, accepted: list[str]) -> bool:
    """Constant-time membership test, so timing cannot reveal a valid prefix."""
    if not presented:
        return False
    matched = False
    for candidate in accepted:
        # Deliberately no early exit: compare against every key so total work
        # is independent of which key (if any) matched.
        if hmac.compare_digest(presented, candidate):
            matched = True
    return matched


_PRIVATE_PATTERNS = (
    re.compile(r"^127\."),
    re.compile(r"^10\."),
    re.compile(r"^192\.168\."),
    re.compile(r"^172\.(1[6-9]|2\d|3[01])\."),
    re.compile(r"^169\.254\."),
    re.compile(r"^0\."),
    re.compile(r"^::1$"),
    re.compile(r"^fe80:", re.IGNORECASE),
    re.compile(r"^fc00:", re.IGNORECASE),
)

_LOCAL_HOSTS = frozenset({"localhost", "metadata.google.internal", "instance-data"})


def is_private_host(host: str | None) -> bool:
    if not host:
        return True
    lowered = host.strip("[]").lower()
    if lowered in _LOCAL_HOSTS or lowered.endswith(".localhost") or lowered.endswith(".internal"):
        return True
    return any(pattern.match(lowered) for pattern in _PRIVATE_PATTERNS)


def assert_public_url(url: str, *, allow_private: bool = False) -> str:
    """Reject URLs that would let a scraped page pivot into our own network.

    Connectors follow links found in third-party HTML; without this an attacker
    who controls a listing page could point us at ``http://169.254.169.254/``
    and exfiltrate cloud credentials (SSRF).
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError(
            "Rejected URL: unsupported scheme.",
            field="url",
            context={"scheme": parsed.scheme, "url": redact_url(url)},
        )
    if not allow_private and is_private_host(parsed.hostname):
        raise ValidationError(
            "Rejected URL: resolves to a private or link-local host.",
            field="url",
            context={"host": parsed.hostname},
        )
    return url
