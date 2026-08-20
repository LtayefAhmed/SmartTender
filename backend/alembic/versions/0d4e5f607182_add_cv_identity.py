"""Give a CV a name to show.

A shortlist that reads `17111768.pdf` is one nobody can discuss. The label is
resolved once at extraction time rather than in the interface, so every reader
— the modal, an export, a future notification — says the same thing.

Nullable throughout: public-dataset resumes are anonymised and genuinely have
no name, and inventing one would be worse than showing a job title.

Revision ID: 0d4e5f607182
Revises: 0c3d4e5f6071
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0d4e5f607182"
down_revision: str | None = "0c3d4e5f6071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cvs", sa.Column("display_name", sa.String(length=160), nullable=True))
    op.add_column("cvs", sa.Column("headline", sa.String(length=240), nullable=True))
    op.add_column("cvs", sa.Column("identity_source", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("cvs", "identity_source")
    op.drop_column("cvs", "headline")
    op.drop_column("cvs", "display_name")
