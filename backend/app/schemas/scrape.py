"""Scraping job request/response models."""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.schemas.filters import TenderFilters

__all__ = ["ConnectorRunRead", "ScrapeJobRead", "ScrapeRequest", "SourceRead"]


class ScrapeRequest(BaseModel):
    """Body of ``POST /scrape`` — the manual, filtered search launch."""

    model_config = ConfigDict(extra="forbid")

    connectors: list[str] = Field(
        default_factory=list,
        description="Connector keys to run. Empty means every available source.",
    )
    filters: TenderFilters = Field(default_factory=TenderFilters)
    #: Reserved for a future synchronous preview mode; the current API is
    #: always asynchronous, and the field exists so clients can be explicit.
    wait: bool = Field(default=False, description="Must be false: scraping is always async.")


class ConnectorRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid_module.UUID
    connector_key: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    pages_fetched: int = 0
    http_requests: int = 0
    http_retries: int = 0
    items_found: int = 0
    items_ingested: int = 0
    items_duplicate: int = 0
    items_rejected: int = 0
    items_failed: int = 0
    error_type: str | None = None
    error_message: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ScrapeJobRead(BaseModel):
    """Job status — what a client polls after a 202."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid_module.UUID
    trigger: str
    status: str
    requested_connectors: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    requested_by: str | None = None
    schedule_id: uuid_module.UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    connectors_total: int = 0
    connectors_succeeded: int = 0
    connectors_failed: int = 0
    connectors_skipped: int = 0
    items_found: int = 0
    items_ingested: int = 0
    items_duplicate: int = 0
    items_rejected: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[ConnectorRunRead] = Field(default_factory=list)
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress(self) -> float:
        if not self.connectors_total:
            return 1.0 if self.status not in ("pending", "running") else 0.0
        done = self.connectors_succeeded + self.connectors_failed + self.connectors_skipped
        return round(min(done / self.connectors_total, 1.0), 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_terminal(self) -> bool:
        return self.status not in ("pending", "running")


class SourceRead(BaseModel):
    """Connector health, as shown in the admin console."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    name: str
    base_url: str | None = None
    country: str | None = None
    strategy: str | None = None
    enabled: bool
    requires_credentials: bool
    health: str
    health_reason: str | None = None
    circuit_state: str
    consecutive_failures: int = 0
    consecutive_empty_runs: int = 0
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error_type: str | None = None
    last_duration_seconds: float | None = None
    last_item_count: int = 0
    total_runs: int = 0
    total_failures: int = 0
    total_items_found: int = 0
    total_items_ingested: int = 0
    total_duplicates: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def success_rate(self) -> float | None:
        if not self.total_runs:
            return None
        return round((self.total_runs - self.total_failures) / self.total_runs, 4)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duplicate_ratio(self) -> float | None:
        if not self.total_items_found:
            return None
        return round(self.total_duplicates / self.total_items_found, 4)
