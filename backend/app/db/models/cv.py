"""An imported candidate CV.

Reconstructed from migration ``0f0ac0e85b38_add_cvs`` and from the router that
writes it: the module was referenced by ``app.api.routers.cvs`` but never
committed, which left ``app.main`` unable to import at all. Column names,
lengths and nullability are taken from the migration so the mapping and the
schema cannot drift apart.

Matching against tenders is a later phase. This row exists so a CV can be
imported, stored and listed now, and vectorised without re-uploading later.
"""

from __future__ import annotations

import uuid as uuid_module

from sqlalchemy import Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

__all__ = ["CV"]


class CV(Base, TimestampMixin):
    """A CV file held in object storage, with its provenance."""

    __tablename__ = "cvs"
    __table_args__ = (
        Index("ix_cvs_sha256", "sha256"),
        Index("ix_cvs_uploaded_by", "uploaded_by"),
        {"comment": "Imported candidate CVs, awaiting the matching module."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )

    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Indexed, not unique: the same CV legitimately arrives twice — a candidate
    #: re-sent by two recruiters is one document with two histories.
    sha256: Mapped[str | None] = mapped_column(String(64))

    #: ``upload`` | ``link`` — how the file reached us.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="upload")
    source_url: Mapped[str | None] = mapped_column(String(1024))
    uploaded_by: Mapped[str | None] = mapped_column(String(128))
