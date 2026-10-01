"""The document template library, held as data.

The parcours asks that the Bureau d'Études extend the format library itself,
and Inetum's solution slide names the formats: BEI, BAD, Bureau d'Étude. Both
mean the same thing — a new funder format must be a file to upload, not a
release to ship.

A template is therefore a row plus an object in storage. The placeholders it
asks for are resolved at upload and stored, so the library can be checked
against the render catalogue without opening every .docx, and so a template
demanding something the platform cannot supply is refused at the door rather
than at generation time.

Versions are rows, not overwrites: a document produced last month came from one
specific version, and explaining it later requires that version to still exist.

Revision ID: 1071829304b5
Revises: 0f60718293a4
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1071829304b5"
down_revision: str | None = "0f60718293a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_templates",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="cv"),
        sa.Column("funder", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("storage_bucket", sa.String(128), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column(
            "variables",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("uploaded_by", sa.String(128), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("tenant_id", "key", "version", name="uq_template_key_version"),
        comment="Uploaded .docx templates — the document library, as data.",
    )
    op.create_index("ix_document_templates_funder", "document_templates", ["funder"])
    op.create_index("ix_document_templates_sha256", "document_templates", ["sha256"])
    op.create_index(
        "ix_document_templates_tenant_kind", "document_templates", ["tenant_id", "kind"]
    )
    op.create_index(
        "ix_document_templates_active",
        "document_templates",
        ["tenant_id", "key", "is_active"],
    )


def downgrade() -> None:
    op.drop_table("document_templates")
