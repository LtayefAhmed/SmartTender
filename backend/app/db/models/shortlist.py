"""A candidate ranking, frozen at the moment a human looked at it.

Until now the matching module computed and returned; nothing was kept.
``GET /tenders/{id}/candidates`` re-ranks on every call, which is right for
browsing — a ranking is per tender and never absolute, so there is no "best
CV" to cache — and wrong for everything downstream.

The reason is not storage, it is provenance. Document generation starts from
"the profiles a human retained", and a re-import, a re-index or a weight change
between the approval and the generation would silently produce a different
list. A validator would have approved one selection and the platform would have
acted on another, with nothing in the record to show the substitution.

So a shortlist stores **the answer and the question together**: the scores, the
requirements they were measured against, the technologies demanded, and the
weights in force. Recomputing it later may well give a different result; that
is informative, and it is only visible because the original was kept.

Snapshots are deliberately denormalised. ``label`` is copied rather than joined
because a CV can be re-imported under a new display name, and the audit trail
must show what the validator actually read.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ShortlistDecision, ShortlistStatus
from app.db.base import Base, JSONType, StringArray, TimestampMixin

__all__ = ["Shortlist", "ShortlistEntry"]


class Shortlist(Base, TimestampMixin):
    """One capture of the candidate ranking for one tender."""

    __tablename__ = "shortlists"
    __table_args__ = (
        Index("ix_shortlists_tenant_tender", "tenant_id", "tender_id"),
        Index("ix_shortlists_status", "tenant_id", "status"),
        {"comment": "Frozen candidate rankings, with the question they answered."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )

    #: Same boundary as CVs, and for the same reason: a selection names an
    #: organisation's people. Tenders are public, shortlists are not.
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

    #: Nullable so a capture outlives the tender purge. `purge_expired_tenders`
    #: removes notices on a retention clock; the record that we shortlisted
    #: five consultants for a bid must not disappear with it.
    tender_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="SET NULL"), index=True
    )
    #: Copied for the same reason — a purged tender leaves a shortlist that can
    #: still say what it was for.
    tender_title: Mapped[str | None] = mapped_column(String(1024))

    label: Mapped[str | None] = mapped_column(String(255))
    #: No ``index=True``: SQLAlchemy would name the implicit index
    #: ``ix_shortlists_status``, which collides with the composite above. The
    #: composite answers the query anyway — status is only ever read within a
    #: tenant.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ShortlistStatus.OPEN.value
    )

    # --- the question, frozen with the answer -----------------------------
    #: The requirement passages the ranking was measured against.
    requirements: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    #: Technologies demanded — the list the veto was computed from.
    required_technologies: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )
    #: What the model read out of the requirement passages, or null when it was
    #: unavailable. Null and "nothing structured" are different facts.
    structured_requirements: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    #: Weight version and values. A score means nothing without the scale.
    weights: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    #: Totals over everything considered, not over the stored slice. The
    #: interface once displayed "0 écartés" on a run where 244 profiles had
    #: been vetoed, because refused profiles score zero and never reach a
    #: top-20 cut. The count and the sample are two answers.
    kept_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    vetoed_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- provenance --------------------------------------------------------
    created_by: Mapped[str | None] = mapped_column(String(128), index=True)
    validated_by: Mapped[str | None] = mapped_column(String(128))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    entries: Mapped[list[ShortlistEntry]] = relationship(
        back_populates="shortlist",
        cascade="all, delete-orphan",
        order_by="ShortlistEntry.rank",
        lazy="selectin",
    )

    @classmethod
    def owned_by(cls, tenant: str) -> Any:
        """The only supported way to read shortlists.

        Same reasoning as ``CV.owned_by``, and the stakes are higher here: a
        shortlist names an organisation's people *and* which bid they were
        being put forward for. A forgotten ``WHERE`` returns more rows rather
        than failing, so the filter is made structural instead of remembered.
        """
        from sqlalchemy import select

        return select(cls).where(cls.tenant_id == tenant)


class ShortlistEntry(Base, TimestampMixin):
    """One candidate inside a capture, with the evidence as it was read."""

    __tablename__ = "shortlist_entries"
    __table_args__ = (
        Index("ix_shortlist_entries_shortlist", "shortlist_id", "rank"),
        Index("ix_shortlist_entries_decision", "shortlist_id", "decision"),
        {"comment": "One ranked candidate, with the justification frozen."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    shortlist_id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shortlists.id", ondelete="CASCADE"), nullable=False
    )
    #: Nullable for the same reason as `tender_id`: a CV can be deleted at a
    #: candidate's request, and the record that a decision was taken must
    #: survive the deletion of what it was taken about.
    cv_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cvs.id", ondelete="SET NULL"), index=True
    )

    #: 1-based, in the order the ranking produced. Kept explicitly rather than
    #: derived from the score, so re-reading the row cannot silently reorder
    #: what the validator saw.
    rank: Mapped[int] = mapped_column(Integer, nullable=False)

    #: What the validator actually read on screen. Denormalised on purpose.
    label: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    filename: Mapped[str | None] = mapped_column(String(512))

    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    similarity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    coverage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    technology_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    matched_technologies: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )
    missing_technologies: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )

    #: Whether the ranking refused this profile, and why. Stored rather than
    #: dropped: "we looked and ruled it out" is a finding, and a shortlist that
    #: shows only its winners cannot be argued with.
    vetoed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    veto_reason: Mapped[str | None] = mapped_column(String(512))

    #: The passages that justified the score, as they were scored.
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, nullable=False, default=list
    )

    # --- the human decision ------------------------------------------------
    decision: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ShortlistDecision.PENDING.value
    )
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why a human overrode the ranking, in their words. The most valuable
    #: column in this table for Phase 8: a rejection with a reason is labelled
    #: training data, a rejection without one is noise.
    decision_note: Mapped[str | None] = mapped_column(Text)

    shortlist: Mapped[Shortlist] = relationship(back_populates="entries")
