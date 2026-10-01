"""Keep the automatic review beside the document it judged.

A document that passed the checks and a document nobody checked are different
things, and the difference matters most to the person about to sign. Logging
it would put the answer somewhere a reviewer never looks.

Revision ID: 1529304b5c70
Revises: 1429304b5c6f
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1529304b5c70"
down_revision: str | None = "1429304b5c6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generated_documents",
        sa.Column(
            "qa",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("generated_documents", "qa")
