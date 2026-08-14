"""Source — the runtime state of one connector.

The YAML file under ``config/connectors/`` describes *how* to scrape a portal;
this table records *what happened* when we did. Keeping them separate means a
selector fix is a config edit with no migration, while health history survives
config changes.

The circuit-breaker fields live here rather than only in Redis so that breaker
state survives a Redis flush and is visible in the dashboard and in incident
reviews.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import CircuitState, SourceHealth
from app.db.base import Base, IntPrimaryKeyMixin, JSONType, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.job import ConnectorRun
    from app.db.models.tender import Tender


class Source(Base, IntPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "sources"
    __table_args__ = (
        Index("ix_sources_health_enabled", "health", "enabled"),
        {"comment": "One row per connector; holds runtime health and breaker state."},
    )

    #: Matches the ``key`` field of the connector YAML and the registry name.
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    country: Mapped[str | None] = mapped_column(String(128), index=True)
    language: Mapped[str | None] = mapped_column(String(16))
    strategy: Mapped[str | None] = mapped_column(String(16))

    #: Operator switch. Independent of ``enabled`` in the YAML: an operator can
    #: silence a misbehaving source from the admin console without editing files.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_credentials: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    health: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SourceHealth.UNKNOWN.value, index=True
    )
    health_reason: Mapped[str | None] = mapped_column(Text)

    # --- circuit breaker ---------------------------------------------------
    circuit_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CircuitState.CLOSED.value
    )
    circuit_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_successes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_empty_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- rolling statistics ------------------------------------------------
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    last_duration_seconds: Mapped[float | None] = mapped_column(Float)
    last_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    total_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_items_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_items_ingested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Checksum of the YAML that produced this row. A mismatch on boot means the
    #: config changed, which is worth surfacing next to a sudden health drop.
    config_checksum: Mapped[str | None] = mapped_column(String(64))
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    runs: Mapped[list[ConnectorRun]] = relationship(
        back_populates="source", cascade="all, delete-orphan", lazy="noload"
    )
    tenders: Mapped[list[Tender]] = relationship(back_populates="source", lazy="noload")

    @property
    def success_rate(self) -> float | None:
        if not self.total_runs:
            return None
        return round((self.total_runs - self.total_failures) / self.total_runs, 4)

    @property
    def duplicate_ratio(self) -> float | None:
        if not self.total_items_found:
            return None
        return round(self.total_duplicates / self.total_items_found, 4)
