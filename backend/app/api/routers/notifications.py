"""In-app notifications and user notification preferences."""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, pagination, require_principal
from app.core.enums import NotificationStatus, RelevanceBand
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.db.models.notification import Notification, UserPreference
from app.schemas.common import Page, PaginationParams

logger = get_logger(__name__)
router = APIRouter(tags=["notifications"])


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid_module.UUID
    tender_id: uuid_module.UUID | None = None
    channel: str
    status: str
    subject: str | None = None
    body: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    match_reason: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    sent_at: datetime | None = None
    read_at: datetime | None = None


class PreferenceUpsert(BaseModel):
    """Notification targeting rules for one user.

    Every list follows the same rule: **empty means "no restriction on this
    dimension"**, not "match nothing". A brand-new user therefore receives
    everything above their relevance floor rather than silence.
    """

    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    display_name: str | None = None
    team: str | None = None
    company: str | None = None
    active: bool = True
    sectors: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    connectors: list[str] = Field(default_factory=list)
    buyers: list[str] = Field(default_factory=list)
    cpv_codes: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=lambda: ["in_app", "email"])
    min_relevance_band: RelevanceBand = RelevanceBand.RELEVANT
    min_score: float | None = Field(default=None, ge=0, le=1)
    min_budget: float | None = Field(default=None, ge=0)
    digest_frequency: str = "immediate"
    max_notifications_per_day: int = Field(default=50, ge=1, le=1000)


class PreferenceRead(PreferenceUpsert):
    model_config = ConfigDict(from_attributes=True)

    id: uuid_module.UUID
    user_id: str


@router.get(
    "/notifications",
    response_model=Page[NotificationRead],
    summary="List my notifications",
)
async def list_notifications(
    session: AsyncSession = Depends(get_session),
    params: PaginationParams = Depends(pagination),
    unread_only: bool = Query(default=False),
    principal: Principal = Depends(require_principal),
) -> Page[NotificationRead]:
    conditions = [Notification.user_id == principal.identity]
    if unread_only:
        conditions.append(Notification.read_at.is_(None))

    total = (
        await session.execute(select(func.count(Notification.id)).where(*conditions))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Notification)
                .where(*conditions)
                .order_by(desc(Notification.created_at))
                .offset(params.offset)
                .limit(params.page_size)
            )
        )
        .scalars()
        .all()
    )
    return Page.build([NotificationRead.model_validate(row) for row in rows], total, params)


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationRead,
    summary="Mark a notification read",
)
async def mark_read(
    notification_id: uuid_module.UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> NotificationRead:
    row = await session.get(Notification, notification_id)
    if row is None or row.user_id != principal.identity:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    row.status = NotificationStatus.READ.value
    row.read_at = utc_now()
    return NotificationRead.model_validate(row)


@router.get(
    "/preferences",
    response_model=PreferenceRead,
    summary="Get my notification preferences",
)
async def get_preferences(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> PreferenceRead:
    row = (
        await session.execute(
            select(UserPreference).where(UserPreference.user_id == principal.identity)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="No preferences are configured for this user yet.",
        )
    return PreferenceRead.model_validate(row)


@router.put(
    "/preferences",
    response_model=PreferenceRead,
    summary="Create or replace my notification preferences",
)
async def upsert_preferences(
    payload: PreferenceUpsert,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> PreferenceRead:
    row = (
        await session.execute(
            select(UserPreference).where(UserPreference.user_id == principal.identity)
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserPreference(user_id=principal.identity)
        session.add(row)

    for field, value in payload.model_dump().items():
        setattr(row, field, value.value if hasattr(value, "value") else value)

    await session.flush()
    logger.info("api.preferences_updated", actor=principal.identity)
    return PreferenceRead.model_validate(row)
