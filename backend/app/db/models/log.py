"""Execution log — the queryable audit trail.

structlog output goes to stdout and on to whatever log aggregator the platform
runs. This table is different and complementary: it holds the small number of
*decisions* that must remain answerable from SQL months later, without
depending on log retention.

    "Why was this tender never shown?"       -> one query on tender_id
    "When did the TUNEPS selector break?"    -> one query on connector+event
    "What did this scheduled run actually do?" -> one query on job_id

Append-only by convention; nothing in the codebase updates or deletes a row
except the retention job.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType

#: SQLite only auto-increments a column declared exactly ``INTEGER PRIMARY
#: KEY``; a BIGINT primary key there is left NULL on insert. PostgreSQL keeps
#: the 64-bit range this table will eventually need.
_AutoBigInt = BigInteger().with_variant(Integer(), "sqlite")


class ExecutionLog(Base):
    __tablename__ = "execution_logs"
    __table_args__ = (
        Index("ix_execution_logs_ts", "ts"),
        Index("ix_execution_logs_tender_ts", "tender_id", "ts"),
        Index("ix_execution_logs_job_ts", "job_id", "ts"),
        Index("ix_execution_logs_connector_event", "connector", "event"),
        Index("ix_execution_logs_level_ts", "level", "ts"),
        {"comment": "Append-only audit trail of pipeline decisions."},
    )

    id: Mapped[int] = mapped_column(_AutoBigInt, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    #: Dotted, stable event name (``dedup.rejected``, ``connector.selector_broken``).
    #: Queried far more often than the free-text message, so keep it stable.
    event: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32), index=True)

    connector: Mapped[str | None] = mapped_column(String(64))
    tender_id: Mapped[uuid_module.UUID | None] = mapped_column(Uuid(as_uuid=True))
    job_id: Mapped[uuid_module.UUID | None] = mapped_column(Uuid(as_uuid=True))
    run_id: Mapped[uuid_module.UUID | None] = mapped_column(Uuid(as_uuid=True))
    task_id: Mapped[str | None] = mapped_column(String(128))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor: Mapped[str | None] = mapped_column(String(128))

    url: Mapped[str | None] = mapped_column(String(1024))
    message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    error_type: Mapped[str | None] = mapped_column(String(64), index=True)
    traceback: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
