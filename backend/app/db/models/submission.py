"""Bid submissions and their outcomes.

The record of what we actually did with a tender: whether we bid, and whether
we won. Two things depend on it.

**Scoring.** The ``historical_success`` criterion asks "how often do we win with
this buyer, in this sector?". Until this table has rows, that criterion holds
its neutral prior — which is correct, not a placeholder: a scorer that invents
a win rate from no data is worse than one that abstains.

**The rest of the platform.** Modules 2 and 3 (CV matching, document
generation) attach to a submission, so this is the seam where Module 1 stops
and the response workflow begins.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import SubmissionOutcome, SubmissionStatus
from app.db.base import Base, JSONType, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.tender import Tender


class Submission(Base, TimestampMixin):
    __tablename__ = "submissions"
    __table_args__ = (
        # The exact shape the historical-success scorer queries.
        Index("ix_submissions_buyer_outcome", "buyer", "outcome"),
        Index("ix_submissions_sector_outcome", "sector", "outcome"),
        Index("ix_submissions_tender", "tender_id"),
        {"comment": "Our bid on a tender, and how it turned out."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    tender_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="SET NULL"), index=True
    )

    #: Denormalised from the tender so the statistics survive the tender being
    #: purged, and so the scorer's query needs no join.
    buyer: Mapped[str | None] = mapped_column(String(512), index=True)
    sector: Mapped[str | None] = mapped_column(String(255), index=True)
    country: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SubmissionStatus.DRAFT.value, index=True
    )
    #: pending until the award is published — which is often months later.
    outcome: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SubmissionOutcome.PENDING.value, index=True
    )

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    bid_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    winning_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    winner_name: Mapped[str | None] = mapped_column(String(512))

    #: Why we lost, or why we chose not to bid. The most useful field in the
    #: table for anyone trying to improve the win rate.
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(128), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    tender: Mapped[Tender | None] = relationship(lazy="noload")

    @property
    def is_decided(self) -> bool:
        return self.outcome in {
            SubmissionOutcome.WON.value,
            SubmissionOutcome.LOST.value,
            SubmissionOutcome.CANCELLED.value,
        }
