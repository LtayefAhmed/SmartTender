"""Identity, canonicalisation and hashing.

The UUID4 minted here is the master identifier for a tender: it names the
object in MinIO, keys every Celery task, appears in every log line and audit
entry, and is what makes each pipeline stage idempotent. A task that is
replayed after a crash recomputes the same key and overwrites the same object
rather than creating a second one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "TENDER_NAMESPACE",
    "as_utc",
    "canonicalize_url",
    "content_fingerprint",
    "deterministic_uuid",
    "idempotency_key",
    "new_tender_uuid",
    "normalize_text",
    "sha256_bytes",
    "sha256_text",
    "utc_now",
]

#: Stable namespace for deterministic UUIDs. Never change it: doing so would
#: make every previously derived identifier unreachable.
TENDER_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

_DEFAULT_PORTS = {"http": 80, "https": 443}
_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def new_tender_uuid() -> uuid.UUID:
    """Mint the master identifier for a newly discovered tender."""
    return uuid.uuid4()


def deterministic_uuid(*parts: str) -> uuid.UUID:
    """Derive a stable UUID5 from its inputs.

    Used where a *replayed* task must produce the identifier it produced the
    first time — for example an attachment's key, derived from the parent
    tender UUID plus the document URL.
    """
    seed = "\x1f".join(str(part) for part in parts)
    return uuid.uuid5(TENDER_NAMESPACE, seed)


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp. The only clock the platform reads."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to aware UTC, assuming UTC when it is naive.

    Every timestamp the platform *writes* is aware, but not every driver hands
    one back that way. Arithmetic between an aware and a naive datetime raises,
    so a single naive value read from the database would otherwise turn a
    routine listing response into a 500.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# URL canonicalisation
# ---------------------------------------------------------------------------
def canonicalize_url(
    url: str,
    *,
    strip_params: Iterable[str] = (),
    strip_fragment: bool = True,
    lowercase_host: bool = True,
    drop_default_port: bool = True,
    drop_trailing_slash: bool = True,
    sort_query: bool = True,
) -> str:
    """Reduce a URL to the canonical form used for exact duplicate detection.

    Two URLs that address the same tender must produce the same string here —
    that equality is the cheapest and most reliable duplicate signal we have.
    Session ids, tracking parameters and fragment anchors are noise and are
    removed; everything else is preserved, because a stripped parameter that
    actually selected the document would silently merge distinct tenders.
    """
    if not url:
        return ""

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = (parts.hostname or "").lower() if lowercase_host else (parts.hostname or "")

    netloc = host
    if parts.port and not (drop_default_port and _DEFAULT_PORTS.get(scheme) == parts.port):
        netloc = f"{host}:{parts.port}"
    # Userinfo is never part of a document's identity.

    path = parts.path or "/"
    if drop_trailing_slash and len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    strip_set = {p.lower() for p in strip_params}
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in strip_set
    ]
    if sort_query:
        query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)

    fragment = parts.fragment
    if strip_fragment and not _fragment_is_a_route(fragment):
        fragment = ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def _fragment_is_a_route(fragment: str) -> bool:
    """Whether a fragment addresses a resource rather than a spot on a page.

    Single-page apps that use hash routing put the entire route after the ``#``:
    ``https://app.j360.info/#/announce/55822711``. Stripping that as noise
    leaves every record on the site sharing one canonical URL, and duplicate
    detection then silently merges an entire portal into a single tender — the
    failure is invisible because the pipeline reports success.

    An ordinary anchor (``#results``, ``#top``) really is noise and is still
    dropped. The distinction is the leading ``/`` or ``!``, which is what every
    hash-routing convention uses and what no anchor name starts with.
    """
    return fragment.startswith(("/", "!"))


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text(
    text: str | None,
    *,
    lowercase: bool = True,
    collapse_whitespace: bool = True,
    strip_punctuation: bool = True,
    strip_digits: bool = False,
    strip_accents: bool = True,
) -> str:
    """Project text onto a comparison-stable form.

    Used both for the normalised-content hash and as the input to lexical
    similarity, so that the same tender re-exported by a different producer
    (different casing, spacing, punctuation) still collides.
    """
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", text)
    if strip_accents:
        value = "".join(
            ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
        )
    if lowercase:
        value = value.lower()
    if strip_punctuation:
        value = _PUNCTUATION.sub(" ", value)
    if strip_digits:
        value = re.sub(r"\d+", " ", value)
    if collapse_whitespace:
        value = _WHITESPACE.sub(" ", value)
    return value.strip()


def content_fingerprint(raw: bytes | None = None, text: str | None = None) -> dict[str, str]:
    """Compute both duplicate hashes for a document.

    ``raw_sha256`` catches byte-identical re-downloads. ``text_sha256`` catches
    the far more common case of the same notice re-published with different
    metadata, headers, or export timestamps.
    """
    fingerprint: dict[str, str] = {}
    if raw is not None:
        fingerprint["raw_sha256"] = sha256_bytes(raw)
    if text is not None:
        fingerprint["text_sha256"] = sha256_text(normalize_text(text))
    return fingerprint


def idempotency_key(*parts: str) -> str:
    """Short stable key for "have I already done this?" checks.

    Celery tasks take one of these so that an at-least-once delivery — which
    Redis brokers absolutely will produce — results in at-most-once effect.
    """
    seed = "\x1f".join(str(part) for part in parts if part is not None)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
