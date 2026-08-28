"""add cv_profiles

Revision ID: 99c883c1d267
Revises: 0f0ac0e85b38
Create Date: 2026-08-26 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '99c883c1d267'
down_revision: str | None = '0f0ac0e85b38'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('cv_profiles',
    sa.Column('cv_id', sa.Uuid(), nullable=False),
    sa.Column('age', sa.Integer(), nullable=True),
    sa.Column('experience_years', sa.Integer(), nullable=True),
    sa.Column('education', sa.String(length=160), nullable=True),
    sa.Column('certifications', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('languages', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('skills', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('raw_extraction', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('source_hash', sa.String(length=64), nullable=True),
    sa.Column('source_chars', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['cv_id'], ['cvs.id'], name=op.f('fk_cv_profiles_cv_id_cvs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('cv_id', name=op.f('pk_cv_profiles')),
    comment="Structured fields read from a CV's text by the LLM, cached per CV."
    )
    op.create_index(op.f('ix_cv_profiles_created_at'), 'cv_profiles', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_cv_profiles_created_at'), table_name='cv_profiles')
    op.drop_table('cv_profiles')
