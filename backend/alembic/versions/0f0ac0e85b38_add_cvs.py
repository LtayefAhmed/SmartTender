"""add cvs

Revision ID: 0f0ac0e85b38
Revises: 0001
Create Date: 2026-08-06 10:12:50.546716
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0f0ac0e85b38'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('cvs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('original_filename', sa.String(length=512), nullable=False),
    sa.Column('storage_bucket', sa.String(length=128), nullable=False),
    sa.Column('storage_key', sa.String(length=1024), nullable=False),
    sa.Column('content_type', sa.String(length=128), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('source_url', sa.String(length=1024), nullable=True),
    sa.Column('uploaded_by', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cvs')),
    comment='Imported candidate CVs, awaiting the matching module.'
    )
    op.create_index(op.f('ix_cvs_created_at'), 'cvs', ['created_at'], unique=False)
    op.create_index(op.f('ix_cvs_sha256'), 'cvs', ['sha256'], unique=False)
    op.create_index(op.f('ix_cvs_uploaded_by'), 'cvs', ['uploaded_by'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_cvs_uploaded_by'), table_name='cvs')
    op.drop_index(op.f('ix_cvs_sha256'), table_name='cvs')
    op.drop_index(op.f('ix_cvs_created_at'), table_name='cvs')
    op.drop_table('cvs')
