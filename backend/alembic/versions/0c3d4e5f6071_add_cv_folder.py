"""Keep the folder a CV was imported from.

A firm files its candidates by practice, by client or by seniority, and the
folder import flattened all of that into one pool: `webkitRelativePath` carries
`CVs/SAP/dupont.pdf` and only the filename was ever sent. The arrangement is
information, and rebuilding it by hand across several hundred CVs is not
something anyone will do.

Revision ID: 0c3d4e5f6071
Revises: 0b2c3d4e5f60
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0c3d4e5f6071"
down_revision: str | None = "0b2c3d4e5f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cvs", sa.Column("folder", sa.String(length=512), nullable=True))
    # Composite with the tenant: "list this firm's SAP practice" is the query,
    # and a folder index alone would scan every organisation's rows.
    op.create_index("ix_cvs_folder", "cvs", ["tenant_id", "folder"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cvs_folder", table_name="cvs")
    op.drop_column("cvs", "folder")
