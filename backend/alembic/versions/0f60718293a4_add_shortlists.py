"""Freeze a candidate ranking so a document can be generated from it.

Matching computed and returned; nothing was kept. That is right for browsing —
a ranking is per tender and never absolute — and wrong as an input to document
generation, which starts from "the profiles a human retained". Between the
approval and the generation a CV can be re-imported, the index rebuilt or a
weight changed, and the list would move with nothing in the record to show it.

So the capture stores the answer *and the question*: scores, the requirement
passages they were measured against, the technologies demanded, the weights in
force. Recomputing later may differ; that is informative, and only visible
because the original was kept.

Both foreign keys are ``SET NULL`` rather than ``CASCADE``. A tender is purged
on a retention clock and a CV is deleted at a candidate's request; the record
that a human took a decision must outlive the thing it was taken about.

Revision ID: 0f60718293a4
Revises: 0e5f60718293
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0f60718293a4"
down_revision: str | None = "0e5f60718293"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shortlists",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column(
            "tender_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("tenders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tender_title", sa.String(1024), nullable=True),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column(
            "requirements",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "required_technologies",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "structured_requirements",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "weights",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("kept_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vetoed_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("validated_by", sa.String(128), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
        comment="Frozen candidate rankings, with the question they answered.",
    )
    op.create_index("ix_shortlists_tender_id", "shortlists", ["tender_id"])
    op.create_index("ix_shortlists_created_by", "shortlists", ["created_by"])
    op.create_index("ix_shortlists_tenant_tender", "shortlists", ["tenant_id", "tender_id"])
    op.create_index("ix_shortlists_status", "shortlists", ["tenant_id", "status"])

    op.create_table(
        "shortlist_entries",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "shortlist_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("shortlists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cv_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("cvs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(512), nullable=False, server_default=""),
        sa.Column("filename", sa.String(512), nullable=True),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("similarity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("coverage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("technology_ratio", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "matched_technologies",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "missing_technologies",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("vetoed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("veto_reason", sa.String(512), nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
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
        comment="One ranked candidate, with the justification frozen.",
    )
    op.create_index("ix_shortlist_entries_cv_id", "shortlist_entries", ["cv_id"])
    op.create_index(
        "ix_shortlist_entries_shortlist", "shortlist_entries", ["shortlist_id", "rank"]
    )
    op.create_index(
        "ix_shortlist_entries_decision", "shortlist_entries", ["shortlist_id", "decision"]
    )


def downgrade() -> None:
    op.drop_table("shortlist_entries")
    op.drop_table("shortlists")
