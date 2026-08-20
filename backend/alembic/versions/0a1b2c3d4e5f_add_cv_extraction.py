"""Give a CV its readable text.

A stored CV nothing has read is a file, not a profile. Matching compares a
tender's requirements against a candidate's skills, and those skills only exist
once the PDF has become text. The columns mirror ``tenders`` deliberately: the
two go through the same extractor, and reporting on them should not require
remembering two vocabularies.

Revision ID: 0a1b2c3d4e5f
Revises: 0f0ac0e85b38
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0a1b2c3d4e5f"
down_revision: str | None = "0f0ac0e85b38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cvs", sa.Column("extracted_text", sa.Text(), nullable=True))
    # Existing rows were imported before extraction existed; "pending" is
    # accurate and lets the backfill find them with a plain query.
    op.add_column(
        "cvs",
        sa.Column(
            "extraction_status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column("cvs", sa.Column("extraction_method", sa.String(length=16), nullable=True))
    op.add_column(
        "cvs",
        sa.Column("extraction_chars", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("cvs", sa.Column("extraction_error", sa.Text(), nullable=True))
    op.create_index("ix_cvs_extraction_status", "cvs", ["extraction_status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cvs_extraction_status", table_name="cvs")
    op.drop_column("cvs", "extraction_error")
    op.drop_column("cvs", "extraction_chars")
    op.drop_column("cvs", "extraction_method")
    op.drop_column("cvs", "extraction_status")
    op.drop_column("cvs", "extracted_text")
