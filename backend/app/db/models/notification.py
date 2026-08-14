"""Notifications and the preferences that decide who gets them.

Relevance is not the only filter. A tender is only announced to a user whose
declared interests actually intersect it — otherwise the feature degenerates
into noise everyone mutes, and the platform loses its most valuable signal.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import TYPE_CHECKING, Any

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

from app.core.enums import NotificationChannel, NotificationStatus, RelevanceBand
from app.db.base import Base, JSONType, StringArray, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.tender import Tender


class UserPreference(Base, TimestampMixin):
    __tablename__ = "user_preferences"
    __table_args__ = (
        Index("ix_user_preferences_active", "active"),
        {"comment": "Per-user notification targeting rules."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    #: Identity comes from the platform's auth module; this table only stores
    #: preferences, never credentials.
    user_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    team: Mapped[str | None] = mapped_column(String(128), index=True)
    company: Mapped[str | None] = mapped_column(String(128), index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- targeting: empty list means "no restriction on this dimension" ----
    sectors: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    industries: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    countries: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    keywords: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    connectors: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    buyers: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    cpv_codes: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)

    #: Terms that veto a notification even when everything else matches.
    excluded_keywords: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)

    # --- delivery ----------------------------------------------------------
    channels: Mapped[list[str]] = mapped_column(
        StringArray,
        nullable=False,
        default=lambda: [NotificationChannel.IN_APP.value, NotificationChannel.EMAIL.value],
    )
    min_relevance_band: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RelevanceBand.RELEVANT.value
    )
    min_score: Mapped[float | None] = mapped_column(Float)
    min_budget: Mapped[float | None] = mapped_column(Float)
    #: ``immediate`` sends per tender; ``daily``/``weekly`` batch into a digest.
    digest_frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="immediate")
    #: Hard cap so a scraping burst can never mail-bomb anyone.
    max_notifications_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    quiet_hours_start: Mapped[int | None] = mapped_column(Integer)
    quiet_hours_end: Mapped[int | None] = mapped_column(Integer)

    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_status", "user_id", "status"),
        Index("ix_notifications_tender", "tender_id"),
        Index("ix_notifications_dedup", "user_id", "tender_id", "channel", unique=True),
        {"comment": "One delivery attempt of one tender to one user on one channel."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tender_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="CASCADE")
    )

    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NotificationChannel.IN_APP.value
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NotificationStatus.PENDING.value, index=True
    )

    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    #: Why this user was selected — auditable targeting.
    match_reason: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)

    tender: Mapped[Tender | None] = relationship(back_populates="notifications", lazy="noload")
