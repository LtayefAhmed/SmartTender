"""Record what the platform produced, and from what.

The last link of the chain. Until now a tender could be detected, profiles
ranked, a selection validated — and nothing came out. This is the row that
exists once a document does.

Every foreign key is ``SET NULL``. A CV is deleted at a candidate's request and
a tender is purged on a retention clock; the record that a document was
produced, by whom, from which template version, must outlive both. That is the
whole point of the audit requirement, and a cascade would quietly defeat it.

``template_key`` and ``template_version`` are copied rather than joined for the
same reason: a template can be retired, and the document still has to be able
to say which one made it.

Revision ID: 1229304b5c6d
Revises: 11829304b5c6
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1229304b5c6d"
down_revision: str | None = "11829304b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generated_documents",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column(
            "shortlist_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("shortlists.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "entry_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("shortlist_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "cv_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("cvs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "tender_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "template_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("document_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("template_key", sa.String(128), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="cv"),
        sa.Column("label", sa.String(512), nullable=False, server_default=""),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("storage_bucket", sa.String(128), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column(
            "content_type",
            sa.String(128),
            nullable=False,
            server_default=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("watermarked", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "empty_fields",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("generated_by", sa.String(128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        comment="Documents produced from a validated selection.",
    )
    op.create_index(
        "ix_generated_documents_shortlist_id", "generated_documents", ["shortlist_id"]
    )
    op.create_index("ix_generated_documents_cv_id", "generated_documents", ["cv_id"])
    op.create_index("ix_generated_documents_tender_id", "generated_documents", ["tender_id"])
    op.create_index(
        "ix_generated_documents_shortlist",
        "generated_documents",
        ["shortlist_id", "created_at"],
    )
    op.create_index(
        "ix_generated_documents_tenant_status", "generated_documents", ["tenant_id", "status"]
    )


def downgrade() -> None:
    op.drop_table("generated_documents")
