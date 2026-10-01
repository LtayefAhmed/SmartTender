"""A .docx template, uploaded rather than written.

The parcours calls for "the Bureau d'Études extends the library itself — it is
data, not code", and Inetum's own solution slide names the formats it wants:
BEI, BAD, Bureau d'Étude. Both say the same thing: adding a funder's format
must be dropping a file, not shipping a release.

So a template is a row plus an object in storage. What the platform keeps about
it is what it needs to refuse a bad one early: the placeholders the file asks
for, checked at upload against what the render context can actually supply. A
template demanding an unknown variable is rejected at the door, where the
person who wrote it is still holding it — not at generation time, in front of a
deadline.

Versions are rows, not overwrites. A document generated last month was produced
by a specific version of a specific template, and reproducing it later is only
possible if that version still exists.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import TemplateKind
from app.db.base import Base, StringArray, TimestampMixin

__all__ = ["DocumentTemplate"]


class DocumentTemplate(Base, TimestampMixin):
    """One version of one template."""

    __tablename__ = "document_templates"
    __table_args__ = (
        # A key identifies a template across its versions; the pair is what is
        # unique. Uploading "cv_inetum" again creates version 2 rather than
        # failing or overwriting.
        UniqueConstraint("tenant_id", "key", "version", name="uq_template_key_version"),
        Index("ix_document_templates_tenant_kind", "tenant_id", "kind"),
        Index("ix_document_templates_active", "tenant_id", "key", "is_active"),
        {"comment": "Uploaded .docx templates — the document library, as data."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    #: A template carries a firm's house style and, for a funder format, what
    #: they were asked to produce. Same boundary as CVs and shortlists.
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

    #: Stable slug across versions — ``cv_inetum``, ``cv_bei``.
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TemplateKind.CV.value
    )

    #: The funding institution whose format this is: ``BEI``, ``BAD``, ``BM``.
    #: ``None`` means the firm's own template — the default case, and the only
    #: one measured in our corpus so far, where no tender names a funder.
    funder: Mapped[str | None] = mapped_column(String(64), index=True)

    #: 1-based, incremented on re-upload of the same key.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Exactly one active version per key. Older ones stay readable so a
    #: document generated from them can still be explained.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- the file ----------------------------------------------------------
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)

    #: The placeholders found in the file, resolved at upload. Stored so the
    #: interface can show what a template needs without opening the .docx, and
    #: so a later change to the variable catalogue can be checked against every
    #: template already in the library instead of only against new ones.
    variables: Mapped[list[str]] = mapped_column(StringArray, nullable=False, default=list)

    uploaded_by: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)

    @classmethod
    def owned_by(cls, tenant: str) -> Any:
        """The only supported way to read templates."""
        from sqlalchemy import select

        return select(cls).where(cls.tenant_id == tenant)
