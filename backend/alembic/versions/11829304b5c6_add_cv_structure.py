"""Store the dated career timeline a funder's CV form is built around.

``criteria`` answers "what does this person know". It cannot answer "what did
they do, where, and when", which is the skeleton of every BEI, BAD or bureau
d'études CV form — and the reason the generator had headings it could not fill.

Read once at import, like the criteria beside it: a recruiter generating twenty
CVs cannot wait for twenty documents to be re-parsed, and the answer does not
change between two generations.

JSONB rather than two child tables. The value is written whole, read whole, and
never queried field by field — nothing filters on "worked at X in 2019". Two
tables would buy joins nobody makes and cost a migration for every new field.

Revision ID: 11829304b5c6
Revises: 1071829304b5
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "11829304b5c6"
down_revision: str | None = "1071829304b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cvs",
        sa.Column(
            "structure",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    # No index. Unlike `criteria`, which the profile search filters on with
    # containment queries, this column is only ever read by primary key when a
    # document is generated. A GIN index here would cost writes and answer
    # nothing.


def downgrade() -> None:
    op.drop_column("cvs", "structure")
