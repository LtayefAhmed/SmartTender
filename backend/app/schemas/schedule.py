"""Schedule request/response models."""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.core.enums import ScheduleKind
from app.schemas.filters import TenderFilters

__all__ = ["INTERVAL_PRESETS", "ScheduleCreate", "ScheduleRead", "ScheduleUpdate"]

#: The presets the UI offers. Anything else is still expressible by sending
#: ``interval_seconds`` or a crontab directly — presets are convenience, not a
#: constraint.
INTERVAL_PRESETS: dict[str, int] = {
    "every_15_minutes": 900,
    "every_30_minutes": 1800,
    "hourly": 3600,
    "every_2_hours": 7200,
    "every_4_hours": 14400,
    "every_6_hours": 21600,
    "every_12_hours": 43200,
    "daily": 86400,
    "weekly": 604800,
}


class _ScheduleBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    enabled: bool = True
    connectors: list[str] = Field(
        default_factory=list, description="Empty means every available source."
    )
    filters: TenderFilters = Field(default_factory=TenderFilters)
    timezone: str = "Africa/Tunis"
    queue: str | None = None
    expire_seconds: int | None = Field(
        default=3600,
        ge=0,
        description="Discard a due run older than this instead of stampeding after downtime.",
    )
    skip_if_running: bool = True
    one_off: bool = False
    start_after: datetime | None = None
    expires_at: datetime | None = None


class ScheduleCreate(_ScheduleBase):
    name: str = Field(min_length=1, max_length=128)
    kind: ScheduleKind = ScheduleKind.INTERVAL

    preset: str | None = Field(
        default=None, description=f"One of: {', '.join(INTERVAL_PRESETS)}."
    )
    interval_seconds: int | None = Field(default=None, ge=60, le=2_592_000)

    cron_minute: str | None = None
    cron_hour: str | None = None
    cron_day_of_week: str | None = None
    cron_day_of_month: str | None = None
    cron_month_of_year: str | None = None

    @model_validator(mode="after")
    def _resolve_cadence(self) -> ScheduleCreate:
        if self.preset:
            if self.preset not in INTERVAL_PRESETS:
                raise ValueError(
                    f"Unknown preset '{self.preset}'. Valid: {', '.join(INTERVAL_PRESETS)}."
                )
            self.kind = ScheduleKind.INTERVAL
            self.interval_seconds = INTERVAL_PRESETS[self.preset]

        if self.kind is ScheduleKind.INTERVAL and not self.interval_seconds:
            raise ValueError("An interval schedule needs `interval_seconds` or a `preset`.")
        if self.kind is ScheduleKind.CRONTAB and not self.cron_minute:
            raise ValueError("A crontab schedule needs at least `cron_minute`.")
        return self


class ScheduleUpdate(BaseModel):
    """Partial update. Every field is optional; omitted fields are untouched."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    enabled: bool | None = None
    connectors: list[str] | None = None
    filters: TenderFilters | None = None
    timezone: str | None = None
    queue: str | None = None
    expire_seconds: int | None = Field(default=None, ge=0)
    skip_if_running: bool | None = None
    preset: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    cron_minute: str | None = None
    cron_hour: str | None = None
    cron_day_of_week: str | None = None
    cron_day_of_month: str | None = None
    cron_month_of_year: str | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _apply_preset(self) -> ScheduleUpdate:
        if self.preset:
            if self.preset not in INTERVAL_PRESETS:
                raise ValueError(
                    f"Unknown preset '{self.preset}'. Valid: {', '.join(INTERVAL_PRESETS)}."
                )
            self.interval_seconds = INTERVAL_PRESETS[self.preset]
        return self


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid_module.UUID
    name: str
    description: str | None = None
    enabled: bool
    kind: str
    interval_seconds: int | None = None
    cron_minute: str | None = None
    cron_hour: str | None = None
    cron_day_of_week: str | None = None
    cron_day_of_month: str | None = None
    cron_month_of_year: str | None = None
    timezone: str
    connectors: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    queue: str | None = None
    expire_seconds: int | None = None
    skip_if_running: bool
    one_off: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    total_run_count: int = 0
    last_job_id: uuid_module.UUID | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cadence(self) -> str:
        """Human-readable cadence for the schedule list."""
        if self.kind == ScheduleKind.INTERVAL.value and self.interval_seconds:
            seconds = self.interval_seconds
            for unit_seconds, singular, plural in (
                (86400, "day", "days"),
                (3600, "hour", "hours"),
                (60, "minute", "minutes"),
            ):
                if seconds % unit_seconds == 0:
                    count = seconds // unit_seconds
                    return f"every {singular}" if count == 1 else f"every {count} {plural}"
            return f"every {seconds} seconds"
        return (
            f"cron({self.cron_minute or '*'} {self.cron_hour or '*'} "
            f"{self.cron_day_of_month or '*'} {self.cron_month_of_year or '*'} "
            f"{self.cron_day_of_week or '*'}) {self.timezone}"
        )
