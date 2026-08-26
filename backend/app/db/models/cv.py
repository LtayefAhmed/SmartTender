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
from typing import Any

from sqlalchemy import Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin

__all__ = ["CV"]


class CV(Base, TimestampMixin):
    """A CV file held in object storage, with its provenance."""

    __tablename__ = "cvs"
    __table_args__ = (
        Index("ix_cvs_sha256", "sha256"),
        Index("ix_cvs_uploaded_by", "uploaded_by"),
        Index("ix_cvs_extraction_status", "extraction_status"),
        Index("ix_cvs_tenant", "tenant_id"),
        Index("ix_cvs_folder", "tenant_id", "folder"),
        # Deduplication is per organisation: two firms may legitimately hold
        # the same freelance CV, and neither should learn that from the other.
        Index("ix_cvs_tenant_sha", "tenant_id", "sha256"),
        {"comment": "Imported candidate CVs, awaiting the matching module."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )

    #: The organisation that owns this CV. A tender is a public notice and is
    #: shared; a candidate is not, and two firms on the platform must never see
    #: each other's people. Every read goes through ``CV.owned_by`` so the
    #: filter cannot be forgotten by writing a query that merely looks right.
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Indexed, not unique: the same CV legitimately arrives twice — a candidate
    #: re-sent by two recruiters is one document with two histories.
    sha256: Mapped[str | None] = mapped_column(String(64))

    #: Where the file sat in the folder the user imported, without the
    #: filename: ``CVs/SAP`` for ``CVs/SAP/dupont.pdf``. A firm files its
    #: candidates by practice, by client or by seniority, and that arrangement
    #: is information the platform should keep rather than flatten into one
    #: undifferentiated pool. Null for a single-file or link import, which
    #: genuinely has no folder.
    folder: Mapped[str | None] = mapped_column(String(512))

    #: ``upload`` | ``link`` — how the file reached us.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="upload")
    source_url: Mapped[str | None] = mapped_column(String(1024))
    uploaded_by: Mapped[str | None] = mapped_column(String(128))

    #: Who the CV is about, for display. A shortlist reading "17111768.pdf" is
    #: one nobody can discuss. ``display_name`` is null when the document is
    #: anonymised — a guessed name is worse than none, because a job title
    #: visibly describes while a name is taken as fact.
    display_name: Mapped[str | None] = mapped_column(String(160))
    headline: Mapped[str | None] = mapped_column(String(240))
    #: name | headline | filename — which rung of the fallback produced the
    #: label. Lets a screen style a real name differently, and lets a later
    #: refinement pass target only the weak ones.
    identity_source: Mapped[str | None] = mapped_column(String(16))

    #: Filterable evidence read from the text at extraction time: technologies,
    #: languages, education level, certifications.
    #:
    #: Stored rather than recomputed because a recruiter filtering over several
    #: hundred profiles cannot wait for several hundred documents to be parsed,
    #: and the answer does not change between two searches.
    #:
    #: JSONB rather than five columns: it is read as a whole, written as a
    #: whole, and adding a sixth criterion should not be a migration.
    #: ``JSONType`` rather than raw ``JSONB``: the project already carries a
    #: portable variant that degrades to ``JSON`` on SQLite, which is what the
    #: test suite runs on. Importing the PostgreSQL type directly compiled
    #: fine and broke 149 tests at runtime.
    criteria: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, server_default="{}", default=dict
    )

    #: The readable text, and how it was obtained. A stored CV nothing has read
    #: is a file, not a profile: matching compares requirements against skills,
    #: and skills only exist once the PDF has become text.
    #:
    #: Deferred like ``Tender.extracted_text``: listing fifty CVs must not drag
    #: fifty full documents across the wire for a screen that shows filenames.
    extracted_text: Mapped[str | None] = mapped_column(Text, deferred=True)
    #: pending | extracted | empty | failed | skipped
    extraction_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending"
    )
    #: digital | ocr | mixed | none — an OCR-only CV is a scan, and a scan is
    #: where extraction quality is worth doubting before a match is trusted.
    extraction_method: Mapped[str | None] = mapped_column(String(16))
    extraction_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extraction_error: Mapped[str | None] = mapped_column(Text)

    @classmethod
    def owned_by(cls, tenant: str) -> Any:
        """The only supported way to read CVs.

        Isolation that depends on remembering a ``WHERE`` clause is isolation
        that will be breached by the first query someone writes in a hurry —
        and the breach is silent, because a query missing the filter returns
        *more* rows rather than failing. Routing every read through here makes
        the omission visible at the call site: a ``select(CV)`` with no tenant
        stands out, where a forgotten condition does not.
        """
        from sqlalchemy import select

        return select(cls).where(cls.tenant_id == tenant)
