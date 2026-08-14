"""Tender, its attachments, its scores, and the duplicates it absorbed.

``Tender.id`` is the UUID4 minted at ingestion and is the master identifier
described in :mod:`app.core.identity` — object key, task argument, log
correlation, audit anchor and idempotency token, all the same value.

Duplicate detection relies on three indexed columns (``canonical_url``,
``raw_sha256``, ``text_sha256``) plus a bounded semantic pass. They are unique
where uniqueness is real and merely indexed where collisions are legitimate.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    EntryPoint,
    ProcurementType,
    RelevanceBand,
    TenderPipelineState,
    TenderStatus,
)
from app.db.base import Base, JSONType, StringArray, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.notification import Notification
    from app.db.models.source import Source


class Tender(Base, TimestampMixin):
    __tablename__ = "tenders"
    __table_args__ = (
        # Exact-duplicate lookups. Partial-unique on PostgreSQL would be ideal;
        # a plain unique constraint with NULLs behaves correctly here because
        # PostgreSQL treats NULLs as distinct, so tenders without a canonical
        # URL (manual uploads) never collide with each other.
        UniqueConstraint("canonical_url", name="uq_tenders_canonical_url"),
        Index("ix_tenders_raw_sha256", "raw_sha256"),
        Index("ix_tenders_text_sha256", "text_sha256"),
        Index("ix_tenders_source_external", "source_key", "external_id"),
        # Dashboard's default view: relevant, still open, soonest deadline.
        Index("ix_tenders_band_deadline", "relevance_band", "deadline"),
        Index("ix_tenders_state_created", "pipeline_state", "created_at"),
        Index("ix_tenders_country_sector", "country", "sector"),
        {"comment": "Canonical tender record; PK is the platform-wide master UUID."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )

    # --- provenance --------------------------------------------------------
    source_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    source_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entry_point: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EntryPoint.SCHEDULED_SCRAPE.value, index=True
    )
    source_url: Mapped[str | None] = mapped_column(String(1024))
    canonical_url: Mapped[str | None] = mapped_column(String(1024))
    external_id: Mapped[str | None] = mapped_column(String(255))
    reference: Mapped[str | None] = mapped_column(String(255), index=True)

    # --- content -----------------------------------------------------------
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    buyer: Mapped[str | None] = mapped_column(String(512), index=True)
    funding_organization: Mapped[str | None] = mapped_column(String(512))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    language: Mapped[str | None] = mapped_column(String(16))

    # --- classification ----------------------------------------------------
    country: Mapped[str | None] = mapped_column(String(128), index=True)
    location: Mapped[str | None] = mapped_column(String(255))
    sector: Mapped[str | None] = mapped_column(String(255), index=True)
    category: Mapped[str | None] = mapped_column(String(255))
    cpv_codes: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    procurement_type: Mapped[str] = mapped_column(
        String(48), nullable=False, default=ProcurementType.UNKNOWN.value
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TenderStatus.UNKNOWN.value, index=True
    )

    # --- dates -------------------------------------------------------------
    publication_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # --- money -------------------------------------------------------------
    estimated_budget: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    #: Budget converted to the reference currency purely so that ranking and
    #: filtering can compare tenders across currencies. Never shown as an
    #: authoritative amount.
    budget_reference_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))

    # --- duplicate detection keys -----------------------------------------
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    #: Cached similarity vector for the semantic dedup stage, so a re-check
    #: never re-embeds text that has not changed.
    dedup_vector: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    # --- storage -----------------------------------------------------------
    storage_bucket: Mapped[str | None] = mapped_column(String(128))
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    original_filename: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)

    # --- extracted document text -------------------------------------------
    #: Deferred: this can hold hundreds of kilobytes per tender, and the
    #: dashboard's listing query selects whole Tender rows. Loading it eagerly
    #: would turn a 200-row page into tens of megabytes on the wire.
    extracted_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    #: pending | extracted | empty | failed | skipped
    extraction_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending"
    )
    #: digital | ocr | mixed | none — how the text was obtained.
    extraction_method: Mapped[str | None] = mapped_column(String(16))
    extraction_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extraction_error: Mapped[str | None] = mapped_column(Text)

    # --- pipeline ----------------------------------------------------------
    pipeline_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TenderPipelineState.RECEIVED.value, index=True
    )
    pipeline_error: Mapped[str | None] = mapped_column(Text)
    #: Seen-on counter: incremented every time a duplicate of this tender
    #: arrives, from any source.
    duplicate_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seen_on_sources: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)

    # --- scoring (denormalised current values; history lives in TenderScore) -
    relevance_score: Mapped[float | None] = mapped_column(Float, index=True)
    relevance_band: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RelevanceBand.UNSCORED.value, index=True
    )
    score_profile_version: Mapped[str | None] = mapped_column(String(32))
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    source: Mapped[Source | None] = relationship(back_populates="tenders", lazy="noload")
    documents: Mapped[list[TenderDocument]] = relationship(
        back_populates="tender", cascade="all, delete-orphan", lazy="selectin"
    )
    scores: Mapped[list[TenderScore]] = relationship(
        back_populates="tender", cascade="all, delete-orphan", lazy="noload"
    )
    duplicates: Mapped[list[DuplicateRecord]] = relationship(
        back_populates="canonical", cascade="all, delete-orphan", lazy="noload"
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="tender", cascade="all, delete-orphan", lazy="noload"
    )

    @property
    def days_until_deadline(self) -> float | None:
        if self.deadline is None:
            return None
        from app.core.identity import as_utc, utc_now

        return (as_utc(self.deadline) - utc_now()).total_seconds() / 86400.0


class TenderDocument(Base, TimestampMixin):
    """An attachment (CDC, DCE, annexes) belonging to a tender."""

    __tablename__ = "tender_documents"
    __table_args__ = (
        UniqueConstraint("tender_id", "sha256", name="uq_tender_documents_tender_sha"),
        Index("ix_tender_documents_tender", "tender_id"),
        {"comment": "Attachments downloaded alongside a tender notice."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    tender_id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str | None] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(String(1024))
    storage_bucket: Mapped[str | None] = mapped_column(String(128))
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))

    #: pending | stored | failed | skipped — attachment failures are recorded
    #: and never propagate to the tender itself.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tender: Mapped[Tender] = relationship(back_populates="documents", lazy="noload")


class TenderScore(Base, TimestampMixin):
    """One scoring execution — kept as history, never overwritten.

    Weights change over time. Retaining every execution with the profile
    version and the per-criterion breakdown is what makes a past ranking
    explainable and reproducible months later.
    """

    __tablename__ = "tender_scores"
    __table_args__ = (
        Index("ix_tender_scores_tender_created", "tender_id", "created_at"),
        {"comment": "Immutable history of scoring executions."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    tender_id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False
    )

    profile_name: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    band: Mapped[str] = mapped_column(String(32), nullable=False)

    #: ``{criterion: {value, weight, weighted, explanation}}`` — the evidence
    #: behind the number, rendered directly in the dashboard.
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    #: Weights actually applied, snapshotted so a later config edit cannot
    #: retroactively change what this score meant.
    weights: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    duration_ms: Mapped[float | None] = mapped_column(Float)

    tender: Mapped[Tender] = relationship(back_populates="scores", lazy="noload")


class DuplicateRecord(Base, TimestampMixin):
    """A rejected duplicate and the evidence for the rejection.

    Duplicates are never displayed in the dashboard, but they are never thrown
    away either: the record is what lets an operator answer "why did this
    tender not appear?" and what feeds the ``duplicate_ratio`` metric that
    reveals a source that has started re-publishing everything.
    """

    __tablename__ = "duplicate_records"
    __table_args__ = (
        Index("ix_duplicate_records_canonical", "canonical_tender_id"),
        Index("ix_duplicate_records_strategy_created", "strategy", "created_at"),
        {"comment": "Incoming records rejected as duplicates, with evidence."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    canonical_tender_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE")
    )

    strategy: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    similarity: Mapped[float | None] = mapped_column(Float)
    source_key: Mapped[str | None] = mapped_column(String(64), index=True)
    source_url: Mapped[str | None] = mapped_column(String(1024))
    canonical_url: Mapped[str | None] = mapped_column(String(1024))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(1024))
    job_id: Mapped[uuid_module.UUID | None] = mapped_column(Uuid(as_uuid=True))

    #: The rejected payload, so the decision can be audited or reversed.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    canonical: Mapped[Tender | None] = relationship(back_populates="duplicates", lazy="noload")
