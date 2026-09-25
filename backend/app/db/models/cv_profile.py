"""Structured fields read out of a CV's text, cached per CV.

A CV row holds only raw extracted text — there is no column for age,
experience or certifications, because nothing before this needed one. Reading
those out with an LLM on every search would cost one call per CV in the
corpus; this table is what makes the cost one call per CV *the first time it
is needed*, not once per search.

This is a cache, not history — unlike ``TenderScore``, which is deliberately
append-only so a past ranking stays explainable, a stale profile here is
simply wrong and must be overwritten in place. ``cv_id`` is therefore the
primary key rather than a separate surrogate one.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, StringArray, TimestampMixin

__all__ = ["CVProfile"]


class CVProfile(Base, TimestampMixin):
    """One CV's structured fields, as last read by the LLM."""

    __tablename__ = "cv_profiles"
    __table_args__ = (
        {"comment": "Structured fields read from a CV's text by the LLM, cached per CV."},
    )

    cv_id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cvs.id", ondelete="CASCADE"), primary_key=True
    )

    age: Mapped[int | None] = mapped_column(Integer)
    experience_years: Mapped[int | None] = mapped_column(Integer)
    education: Mapped[str | None] = mapped_column(String(160))
    certifications: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    languages: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)
    skills: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)

    #: The model's answer, unnormalised — kept for audit, since the typed
    #: columns above are a lossy projection of it (e.g. a rejected implausible
    #: age is visible here but not in ``age``).
    raw_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    #: pending | ok | empty | unavailable | failed — mirrors CV.extraction_status's vocabulary.
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    error: Mapped[str | None] = mapped_column(Text)

    #: sha256 of CV.extracted_text at extraction time. A re-imported or
    #: re-OCR'd CV changes this and invalidates the cache without needing a
    #: second timestamp compared against unrelated column edits.
    source_hash: Mapped[str | None] = mapped_column(String(64))
    source_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
