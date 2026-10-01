"""Record whether a model touched a document's wording, and what the guard caught.

The reformulation pass rewrites missions into the tender's vocabulary. It never
adds a fact — every rewritten passage is checked back against its source and
discarded if it names a technology or a year the source did not — but "never
adds a fact" is a property of the code, and a reviewer signing a document is
entitled to see it stated about *their* document.

So the report travels with the row: whether a model was consulted at all, how
many passages it rewrote, how many the guard sent back, and which invented
terms it caught. A log line would not be visible to the person who has to sign.

Revision ID: 1329304b5c6e
Revises: 1229304b5c6d
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1329304b5c6e"
down_revision: str | None = "1229304b5c6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generated_documents",
        sa.Column(
            "adaptation",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("generated_documents", "adaptation")
