"""The second human lock: nothing leaves the platform unapproved.

The parcours puts a person between the generated dossier and the client, and
requires the decision to be traced: who approved it, when, and on a refusal,
why. Until now a document could only be a draft.

``decision_note`` carries the rejection reason, and it is the column the
learning loop will actually use. A refusal with a motive is labelled data; a
refusal without one is noise, and the difference decides whether Phase 8 has
anything to learn from.

Revision ID: 1429304b5c6f
Revises: 1329304b5c6e
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1429304b5c6f"
down_revision: str | None = "1329304b5c6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generated_documents",
        sa.Column("approved_by", sa.String(128), nullable=True),
    )
    op.add_column(
        "generated_documents",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "generated_documents",
        sa.Column("decision_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generated_documents", "decision_note")
    op.drop_column("generated_documents", "approved_at")
    op.drop_column("generated_documents", "approved_by")
