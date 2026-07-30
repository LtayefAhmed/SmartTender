"""Tender read models."""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.core.enums import RelevanceBand

__all__ = ["ScoreBreakdown", "TenderDetail", "TenderDocumentRead", "TenderQuery", "TenderSummary"]


class TenderDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid_module.UUID
    name: str | None = None
    source_url: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    status: str
    downloaded_at: datetime | None = None


class ScoreBreakdown(BaseModel):
    """Per-criterion explanation, rendered directly in the dashboard."""

    model_config = ConfigDict(from_attributes=True)

    profile_version: str
    score: float
    band: str
    breakdown: dict[str, Any] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    computed_at: datetime | None = None


class TenderSummary(BaseModel):
    """Listing row. Deliberately excludes the description and the breakdown —
    a 200-row dashboard page must not carry a megabyte of prose."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid_module.UUID
    title: str
    reference: str | None = None
    buyer: str | None = None
    country: str | None = None
    sector: str | None = None
    source_key: str
    source_url: str | None = None
    status: str
    procurement_type: str
    publication_date: datetime | None = None
    deadline: datetime | None = None
    estimated_budget: Decimal | None = None
    currency: str | None = None
    relevance_score: float | None = None
    relevance_band: str = RelevanceBand.UNSCORED.value
    pipeline_state: str
    duplicate_hits: int = 0
    seen_on_sources: list[str] = Field(default_factory=list)
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def days_until_deadline(self) -> float | None:
        if self.deadline is None:
            return None
        from app.core.identity import as_utc, utc_now

        return round((as_utc(self.deadline) - utc_now()).total_seconds() / 86400.0, 1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_urgent(self) -> bool:
        """Drives the dashboard's deadline warning badge."""
        days = self.days_until_deadline
        return days is not None and 0 <= days <= 7


class TenderDetail(TenderSummary):
    description: str | None = None
    funding_organization: str | None = None
    contact_email: str | None = None
    location: str | None = None
    category: str | None = None
    cpv_codes: list[str] = Field(default_factory=list)
    language: str | None = None
    external_id: str | None = None
    canonical_url: str | None = None
    entry_point: str
    original_filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    storage_key: str | None = None
    pipeline_error: str | None = None
    scored_at: datetime | None = None
    score_profile_version: str | None = None
    documents: list[TenderDocumentRead] = Field(default_factory=list)
    latest_score: ScoreBreakdown | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class TenderQuery(BaseModel):
    """Dashboard listing filters — applied in SQL, not by a connector."""

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(default=None, description="Free text over title, buyer and reference.")
    connectors: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    bands: list[RelevanceBand] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    min_score: float | None = Field(default=None, ge=0, le=1)
    deadline_before: datetime | None = None
    deadline_after: datetime | None = None
    created_after: datetime | None = None
    #: Hide tenders whose deadline has passed. Defaults on: an expired tender
    #: is history, and history should not be the default view.
    only_open: bool = True
    sort: str = Field(
        default="-relevance_score",
        description="Field name with an optional '-' prefix for descending order.",
    )
