"""Partition CVs by organisation.

A tender is a public notice and is shared by everyone on the platform. A
candidate is not: two firms must never see each other's people, and the
boundary costs almost nothing to draw while the table holds three rows.

Existing rows are assigned to ``default``, which is also what a request that
names no organisation resolves to — so nothing changes for a single-tenant
deployment.

Revision ID: 0b2c3d4e5f60
Revises: 0a1b2c3d4e5f
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0b2c3d4e5f60"
down_revision: str | None = "0a1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cvs",
        sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="default"),
    )
    op.create_index("ix_cvs_tenant", "cvs", ["tenant_id"], unique=False)
    # Deduplication is per organisation: two firms may legitimately hold the
    # same freelance CV, and neither should learn that from the other.
    op.create_index("ix_cvs_tenant_sha", "cvs", ["tenant_id", "sha256"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cvs_tenant_sha", table_name="cvs")
    op.drop_index("ix_cvs_tenant", table_name="cvs")
    op.drop_column("cvs", "tenant_id")
