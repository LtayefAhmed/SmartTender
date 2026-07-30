"""Data objects exchanged inside and across the connector boundary.

The contract is deliberately narrow:

    fetch()      -> FetchedPage      (bytes + provenance, no interpretation)
    parse()      -> list[RawRecord]  (strings pulled out of the page)
    validate()   -> RawRecord        (raises ValidationError if unusable)
    normalize()  -> NormalizedTender (typed, canonical vocabulary)

Everything downstream of the connector — dedup, storage, scoring — only ever
sees ``NormalizedTender``. That is what makes adding a portal a local change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import ProcurementType, TenderStatus
from app.schemas.filters import FilterApplication

__all__ = [
    "ConnectorOutcome",
    "DocumentRef",
    "FetchedPage",
    "ItemFailure",
    "NormalizedTender",
    "RawRecord",
]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class FetchedPage:
    """Raw bytes retrieved from a source, plus how we got them.

    The downloader produces these and knows nothing about tenders; the parser
    consumes them and knows nothing about HTTP. Keeping the seam here is what
    lets a parser be tested against a saved fixture with no network at all.
    """

    url: str
    status_code: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)
    encoding: str | None = None
    elapsed_seconds: float = 0.0
    attempts: int = 1
    from_cache: bool = False
    #: Set when the page came from Playwright rather than httpx.
    rendered: bool = False

    @property
    def text(self) -> str:
        """Decode using the declared charset, never raising on bad bytes."""
        if self.encoding:
            try:
                return self.content.decode(self.encoding, errors="replace")
            except LookupError:
                pass
        for candidate in ("utf-8", "cp1252", "latin-1"):
            try:
                return self.content.decode(candidate)
            except UnicodeDecodeError:
                continue
        return self.content.decode("utf-8", errors="replace")

    @property
    def size_bytes(self) -> int:
        return len(self.content)

    def json(self) -> Any:
        import orjson

        return orjson.loads(self.content)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RawRecord:
    """Untyped strings lifted out of one listing row or detail page.

    Values stay as-is here on purpose: normalisation is a separate, testable
    step, and keeping the raw form means a normalisation bug can be diagnosed
    from a stored record without re-scraping the portal.
    """

    connector_key: str
    source_url: str
    fields: dict[str, Any] = field(default_factory=dict)
    documents: list[dict[str, str]] = field(default_factory=list)
    #: Bytes of the page this record came from, retained only when the
    #: connector is configured to archive originals.
    raw_content: bytes | None = None
    content_type: str | None = None
    page_number: int | None = None

    def get(self, key: str, default: Any = None) -> Any:
        value = self.fields.get(key, default)
        if isinstance(value, str):
            value = value.strip()
            return value or default
        return value

    def value(self, key: str, default: Any = None) -> Any:
        """Read a parsed field, falling back to the record's own attributes.

        ``source_url`` and ``connector_key`` are structural properties of the
        record rather than parsed fields, but a connector's ``required_fields``
        list should be able to name them without the author having to know
        which side of that line a given name falls on.
        """
        found = self.get(key)
        if found not in (None, "", []):
            return found
        attribute = getattr(self, key, None)
        if isinstance(attribute, str):
            attribute = attribute.strip()
        return attribute if attribute not in (None, "", []) else default


@dataclass(slots=True)
class ItemFailure:
    """One record that could not be processed, recorded without aborting the page."""

    url: str | None
    error_type: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "error_type": self.error_type,
            "message": self.message[:500],
            "context": self.context,
        }


class DocumentRef(BaseModel):
    """An attachment advertised by a tender notice."""

    model_config = ConfigDict(extra="forbid")

    url: str
    name: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None


class NormalizedTender(BaseModel):
    """The canonical shape every connector must produce.

    This is the platform's internal contract. Adding a field here is a
    cross-cutting decision; adding a *source* is not.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # --- provenance --------------------------------------------------------
    connector_key: str
    source_url: str | None = None
    canonical_url: str | None = None
    external_id: str | None = None
    reference: str | None = None

    # --- content -----------------------------------------------------------
    title: str
    description: str | None = None
    buyer: str | None = None
    funding_organization: str | None = None
    contact_email: str | None = None
    language: str | None = None

    # --- classification ----------------------------------------------------
    country: str | None = None
    location: str | None = None
    sector: str | None = None
    category: str | None = None
    cpv_codes: list[str] = Field(default_factory=list)
    procurement_type: ProcurementType = ProcurementType.UNKNOWN
    status: TenderStatus = TenderStatus.UNKNOWN

    # --- dates (always timezone-aware UTC after normalisation) -------------
    publication_date: datetime | None = None
    deadline: datetime | None = None

    # --- money -------------------------------------------------------------
    estimated_budget: Decimal | None = None
    currency: str | None = None

    # --- attachments -------------------------------------------------------
    documents: list[DocumentRef] = Field(default_factory=list)

    # --- duplicate keys (filled by the pipeline, not the connector) --------
    raw_sha256: str | None = None
    text_sha256: str | None = None

    #: Full text extracted from the tender's documents. Connectors never set
    #: this — the extraction stage fills it in after ingestion, and scoring
    #: reads it. Deliberately excluded from ``comparison_text`` so that
    #: hundreds of kilobytes of OCR output cannot swamp duplicate detection.
    full_text: str | None = None

    #: Anything portal-specific worth keeping but not worth a column.
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("title must not be blank")
        return cleaned[:1024]

    @field_validator("cpv_codes", mode="before")
    @classmethod
    def _clean_cpv(cls, value: Any) -> Any:
        if not value:
            return []
        if isinstance(value, str):
            value = [value]
        cleaned = []
        for item in value:
            digits = "".join(ch for ch in str(item) if ch.isdigit())
            if digits:
                cleaned.append(digits[:8])
        return sorted(set(cleaned))

    def comparison_text(self) -> str:
        """Text projection used by the semantic duplicate stage."""
        parts = [self.title, self.reference or "", self.buyer or "", self.description or ""]
        return " ".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Run outcome
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ConnectorOutcome:
    """Everything one connector run produced — including how it failed.

    A run *returns* this even when things went wrong. Exceptions are converted
    into fields here by ``BaseConnector.run``, which is the mechanism that stops
    one broken source from propagating an exception into the job.
    """

    connector_key: str
    succeeded: bool = True
    tenders: list[NormalizedTender] = field(default_factory=list)
    item_failures: list[ItemFailure] = field(default_factory=list)

    pages_fetched: int = 0
    http_requests: int = 0
    http_retries: int = 0
    bytes_downloaded: int = 0
    duration_seconds: float = 0.0

    #: Records the selectors actually extracted, before any filtering. Kept
    #: apart from ``items_found`` because a run of zero results is ambiguous
    #: otherwise: "the portal published nothing your keywords match" and "our
    #: selectors broke and we are silently blind" look identical in the report.
    #: Separating them is what makes a broken connector visible.
    records_parsed: int = 0
    items_filtered_out: int = 0
    items_duplicate_in_run: int = 0

    error_type: str | None = None
    error_message: str | None = None
    error_context: dict[str, Any] = field(default_factory=dict)
    #: True when the source was deliberately not run (disabled, no credentials,
    #: circuit open). Skipped is not failed and must not be alerted on.
    skipped: bool = False
    skip_reason: str | None = None

    filter_application: FilterApplication | None = None

    @property
    def items_found(self) -> int:
        return len(self.tenders)

    def to_summary(self) -> dict[str, Any]:
        return {
            "connector": self.connector_key,
            "succeeded": self.succeeded,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "items_found": self.items_found,
            "records_parsed": self.records_parsed,
            "items_filtered_out": self.items_filtered_out,
            "items_duplicate_in_run": self.items_duplicate_in_run,
            "item_failures": len(self.item_failures),
            "pages_fetched": self.pages_fetched,
            "http_requests": self.http_requests,
            "http_retries": self.http_retries,
            "duration_seconds": round(self.duration_seconds, 3),
            "error_type": self.error_type,
            "error_message": self.error_message,
        }
