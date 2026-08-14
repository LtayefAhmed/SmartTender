"""Finding downloadable documents inside a page that was read as text.

A notice's own page is rarely self-contained. The body carries the summary and
the real specification sits behind a link in the middle of it — "Règlement de
consultation - 1,1 Mo" — which is lost the moment the page is flattened to
plain text for scoring. Recovering those links is the difference between
storing a 500-character abstract and storing the cahier des charges.

The hard part is not finding anchors, it is telling a document apart from
navigation. Two independent signals are used, and either is enough:

* the target looks like a file — a document extension, or a path that names a
  download endpoint;
* the anchor's own text announces one — a filename, a size in Ko/Mo, or one of
  the standard French procurement labels.

Requiring both would miss ``<a href="/download/8821">Règlement</a>``, which is
how most portals serve their files. Requiring neither would collect the privacy
policy and the login page on every notice.
"""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlparse

__all__ = ["DocumentLink", "harvest_document_links"]

#: Extensions worth downloading. Deliberately narrow: images and stylesheets
#: are files too, and collecting them would be pure noise.
_DOCUMENT_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".odt", ".rtf",
    ".xls", ".xlsx", ".ods", ".csv",
    ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".tar", ".gz",
)

#: Path fragments used by portals that serve files from an opaque identifier.
_DOWNLOAD_HINTS = ("/download", "/telecharger", "/téléchargement", "/document",
                   "/piece", "/attachment", "/fichier", "/media", "/dce")

#: Anchor texts that name a procurement document even with an opaque href.
_LABEL_HINTS = ("cctp", "ccap", "cahier", "reglement", "règlement", "cdc",
                "dce", "avis", "annexe", "consultation", "bordereau",
                "acte d'engagement", "dossier")

#: "1,1 Mo", "845 Ko", "2.3 MB" — a size next to a link means a file.
_SIZE = re.compile(r"\b\d+([.,]\d+)?\s?(k|m|g)(o|b)\b", re.IGNORECASE)

#: A filename inside the anchor text.
_FILENAME = re.compile(
    r"\b[\w\-. ]+\.(pdf|docx?|odt|rtf|xlsx?|ods|csv|pptx?|zip|rar|7z)\b", re.IGNORECASE
)

_SKIP_SCHEMES = {"mailto", "tel", "javascript", "data", "about"}


class DocumentLink:
    """One candidate document found in a page."""

    __slots__ = ("label", "reason", "url")

    def __init__(self, url: str, label: str, reason: str) -> None:
        self.url = url
        self.label = label
        #: Why this link was kept. Carried so a wrong collection can be
        #: diagnosed from the logs instead of by re-reading the page.
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DocumentLink(url={self.url!r}, label={self.label!r}, reason={self.reason!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DocumentLink) and other.url == self.url


def _looks_like_file(url: str) -> str | None:
    path = urlparse(url).path.lower()
    if path.endswith(_DOCUMENT_SUFFIXES):
        return "extension"
    if any(hint in path for hint in _DOWNLOAD_HINTS):
        return "download-path"
    return None


def _looks_like_label(text: str) -> str | None:
    lowered = text.lower()
    if _FILENAME.search(text):
        return "filename-in-label"
    if _SIZE.search(text):
        return "size-in-label"
    if any(hint in lowered for hint in _LABEL_HINTS):
        return "procurement-label"
    return None


def harvest_document_links(
    html: str | bytes, *, base_url: str | None = None, limit: int = 40
) -> list[DocumentLink]:
    """Return the anchors in ``html`` that plausibly point at a document.

    Relative targets are resolved against ``base_url`` when one is given;
    without it, a relative link cannot be fetched later and is dropped rather
    than recorded as something that will fail.
    """
    from app.connectors.parsing.selectors import parse_html

    try:
        soup = parse_html(html)
    except Exception:
        return []

    found: list[DocumentLink] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith("#"):
            continue
        if (urlparse(href).scheme or "").lower() in _SKIP_SCHEMES:
            continue

        absolute = urljoin(base_url, href) if base_url else href
        if not urlparse(absolute).scheme:
            continue
        # A fragment identifies a position in a document, not a different
        # document; keeping both would download the same file twice.
        absolute = urldefrag(absolute).url
        if absolute in seen:
            continue

        label = anchor.get_text(" ", strip=True)
        reason = _looks_like_file(absolute) or _looks_like_label(label)
        if reason is None:
            continue

        seen.add(absolute)
        found.append(DocumentLink(absolute, label or absolute.rsplit("/", 1)[-1], reason))
        if len(found) >= limit:
            break

    return found
