"""Declarative base and portable column types.

PostgreSQL is the production target, but every type used here carries a SQLite
variant so the full ORM — and therefore the repositories and the API — can be
exercised in-memory by the test suite without standing up a database. That is a
deliberate testability decision, not an invitation to run SQLite in production.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, MetaData, String, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

# Explicit constraint naming makes Alembic autogenerate produce stable,
# reviewable migrations instead of database-assigned random names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata_obj = MetaData(naming_convention=NAMING_CONVENTION)

#: JSONB on PostgreSQL (indexable, queryable), plain JSON elsewhere.
JSONType = JSONB().with_variant(JSON(), "sqlite")

#: Native text[] on PostgreSQL, JSON list on SQLite. Array *containment*
#: queries only work on PostgreSQL; repositories keep those behind a dialect
#: check rather than assuming.
StringArray = ARRAY(String).with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    metadata = metadata_obj

    type_annotation_map = {
        dict[str, Any]: JSONType,
        list[str]: StringArray,
    }

    def to_dict(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        excluded = exclude or set()
        return {
            column.key: getattr(self, column.key)
            for column in self.__table__.columns
            if column.key not in excluded
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = self.__table__.primary_key.columns.keys()
        pairs = ", ".join(f"{k}={getattr(self, k, None)!r}" for k in pk)
        return f"<{type(self).__name__} {pairs}>"


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the database itself.

    Server-side defaults matter here: rows are written by the API, by Celery
    workers and by Alembic data migrations, and only the database sees all of
    them.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class IntPrimaryKeyMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "IntPrimaryKeyMixin",
    "JSONType",
    "StringArray",
    "TimestampMixin",
    "metadata_obj",
]
