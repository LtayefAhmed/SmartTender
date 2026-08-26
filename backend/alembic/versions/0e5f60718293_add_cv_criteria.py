"""Store what a CV evidences, so a search does not re-read every document.

Technologies, languages, education level and certifications, read once at
extraction. A recruiter filtering "Java + anglais + Bac+5" over several hundred
profiles cannot wait for several hundred documents to be parsed, and the answer
does not change between two searches.

JSONB rather than five columns: the value is read and written as a whole, and
adding a sixth criterion should not require a migration.

Revision ID: 0e5f60718293
Revises: 0d4e5f607182
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0e5f60718293"
down_revision: str | None = "0d4e5f607182"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cvs",
        sa.Column(
            "criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    # GIN over the whole document: the queries are containment ("has Java",
    # "speaks anglais"), which is exactly what this index answers.
    op.create_index(
        "ix_cvs_criteria", "cvs", ["criteria"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_cvs_criteria", table_name="cvs")
    op.drop_column("cvs", "criteria")
