"""A document the platform produced.

The last link of the chain: a tender was detected, profiles were ranked, a
human retained some of them, and this is what came out. One row per produced
file, per retained profile, per version.

Everything about the row is written so that the file can be *explained* months
later. Which template, at which version. Which selection, and who validated it.
Which fields the data could not fill. A generated document that cannot be
traced back to the decision that produced it is exactly what the parcours'
audit requirement exists to prevent — and the reason none of the foreign keys
cascade: a CV deleted at a candidate's request, or a tender purged on its
retention clock, must not erase the record that a document was produced and
sent.
"""

from __future__ import annotations

import uuid as uuid_module
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import GeneratedDocumentStatus, TemplateKind
from app.db.base import Base, JSONType, StringArray, TimestampMixin

__all__ = ["GeneratedDocument"]


class GeneratedDocument(Base, TimestampMixin):
    """One produced file."""

    __tablename__ = "generated_documents"
    __table_args__ = (
        Index("ix_generated_documents_shortlist", "shortlist_id", "created_at"),
        Index("ix_generated_documents_tenant_status", "tenant_id", "status"),
        {"comment": "Documents produced from a validated selection."},
    )

    id: Mapped[uuid_module.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid_module.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

    # --- what it was produced from ----------------------------------------
    shortlist_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shortlists.id", ondelete="SET NULL"), index=True
    )
    entry_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("shortlist_entries.id", ondelete="SET NULL")
    )
    cv_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("cvs.id", ondelete="SET NULL"), index=True
    )
    tender_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenders.id", ondelete="SET NULL"), index=True
    )
    template_id: Mapped[uuid_module.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document_templates.id", ondelete="SET NULL")
    )
    #: Copied, not joined. A template can be retired and a version superseded;
    #: the document still has to be able to say which one made it.
    template_key: Mapped[str | None] = mapped_column(String(128))
    template_version: Mapped[int | None] = mapped_column(Integer)

    # --- what it is --------------------------------------------------------
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=TemplateKind.CV.value)
    #: The person the document is about, as printed on it.
    label: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: 1-based. Regenerating for the same profile makes a v2 and supersedes the
    #: one before it, so a diff between versions is possible later.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=GeneratedDocumentStatus.DRAFT.value
    )
    #: A draft says so on its own face. Tracked here as well so a listing can
    #: show it without opening the file.
    watermarked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Template variables the data could not fill. This is the honest part of
    #: the feature: the platform states what is missing rather than letting a
    #: reviewer discover a blank section in a submitted dossier.
    empty_fields: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )

    #: What the reformulation pass did — whether a model was consulted at all,
    #: how many passages it rewrote, and how many the guard sent back.
    #:
    #: Persisted rather than logged because a reviewer signing a document is
    #: entitled to know whether a model touched its wording, and a log line is
    #: not something they can see. `invented_terms` is the audit trail of the
    #: anti-invention rule actually firing.
    adaptation: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, server_default="{}", default=dict
    )

    #: The automatic review of this file: what was found, what blocked, and
    #: which paragraphs had to be put back. Persisted beside the adaptation
    #: report for the same reason — a reviewer about to sign is entitled to
    #: know whether the document was checked and what the check said.
    qa: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, server_default="{}", default=dict
    )

    generated_by: Mapped[str | None] = mapped_column(String(128))

    # --- the human lock ----------------------------------------------------
    #: Who approved it, when, and why it was sent back. The parcours' second
    #: human-in-the-loop: nothing leaves the platform without an explicit,
    #: traced approval, and the responsibility stays with a person.
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Recorded on a rejection, and the most valuable column here for the
    #: learning loop: a refusal with a reason is labelled data, a refusal
    #: without one is noise.
    decision_note: Mapped[str | None] = mapped_column(Text)

    @classmethod
    def owned_by(cls, tenant: str) -> Any:
        """The only supported way to read produced documents."""
        from sqlalchemy import select

        return select(cls).where(cls.tenant_id == tenant)
