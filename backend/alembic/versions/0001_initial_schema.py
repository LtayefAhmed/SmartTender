"""Initial schema — frozen baseline.

Explicit DDL, deliberately NOT ``Base.metadata.create_all()``. A create_all
baseline is generated from whatever the models look like *today*, so it silently
drifts forward as the models change and then collides with the very migrations
that were written to add those changes. This file is a snapshot and must never
be regenerated; schema changes go in a new revision on top of it.

Revision ID: 0001
Revises:
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('execution_logs',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('level', sa.String(length=16), nullable=False),
    sa.Column('event', sa.String(length=128), nullable=False),
    sa.Column('stage', sa.String(length=32), nullable=True),
    sa.Column('connector', sa.String(length=64), nullable=True),
    sa.Column('tender_id', sa.Uuid(), nullable=True),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('task_id', sa.String(length=128), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('actor', sa.String(length=128), nullable=True),
    sa.Column('url', sa.String(length=1024), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Float(), nullable=True),
    sa.Column('error_type', sa.String(length=64), nullable=True),
    sa.Column('traceback', sa.Text(), nullable=True),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_execution_logs')),
    comment='Append-only audit trail of pipeline decisions.'
    )
    op.create_index('ix_execution_logs_connector_event', 'execution_logs', ['connector', 'event'], unique=False)
    op.create_index(op.f('ix_execution_logs_correlation_id'), 'execution_logs', ['correlation_id'], unique=False)
    op.create_index(op.f('ix_execution_logs_error_type'), 'execution_logs', ['error_type'], unique=False)
    op.create_index('ix_execution_logs_job_ts', 'execution_logs', ['job_id', 'ts'], unique=False)
    op.create_index('ix_execution_logs_level_ts', 'execution_logs', ['level', 'ts'], unique=False)
    op.create_index(op.f('ix_execution_logs_stage'), 'execution_logs', ['stage'], unique=False)
    op.create_index('ix_execution_logs_tender_ts', 'execution_logs', ['tender_id', 'ts'], unique=False)
    op.create_index('ix_execution_logs_ts', 'execution_logs', ['ts'], unique=False)
    op.create_table('schedule_change_sentinel',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('last_update', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_schedule_change_sentinel'))
    )
    op.create_table('schedules',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('interval_seconds', sa.Integer(), nullable=True),
    sa.Column('cron_minute', sa.String(length=64), nullable=True),
    sa.Column('cron_hour', sa.String(length=64), nullable=True),
    sa.Column('cron_day_of_week', sa.String(length=64), nullable=True),
    sa.Column('cron_day_of_month', sa.String(length=64), nullable=True),
    sa.Column('cron_month_of_year', sa.String(length=64), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('connectors', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('filters', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('task_name', sa.String(length=255), nullable=False),
    sa.Column('queue', sa.String(length=64), nullable=True),
    sa.Column('expire_seconds', sa.Integer(), nullable=True),
    sa.Column('skip_if_running', sa.Boolean(), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('total_run_count', sa.Integer(), nullable=False),
    sa.Column('last_job_id', sa.Uuid(), nullable=True),
    sa.Column('one_off', sa.Boolean(), nullable=False),
    sa.Column('start_after', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.String(length=128), nullable=True),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(kind = 'interval' AND interval_seconds IS NOT NULL) OR (kind = 'crontab' AND cron_minute IS NOT NULL)", name=op.f('ck_schedules_kind_requires_matching_fields')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_schedules')),
    sa.UniqueConstraint('name', name=op.f('uq_schedules_name')),
    comment='Operator-editable Celery Beat entries.'
    )
    op.create_index(op.f('ix_schedules_created_at'), 'schedules', ['created_at'], unique=False)
    op.create_index(op.f('ix_schedules_enabled'), 'schedules', ['enabled'], unique=False)
    op.create_index('ix_schedules_enabled_next_run', 'schedules', ['enabled', 'next_run_at'], unique=False)
    op.create_index(op.f('ix_schedules_next_run_at'), 'schedules', ['next_run_at'], unique=False)
    op.create_table('sources',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('base_url', sa.String(length=512), nullable=True),
    sa.Column('country', sa.String(length=128), nullable=True),
    sa.Column('language', sa.String(length=16), nullable=True),
    sa.Column('strategy', sa.String(length=16), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('requires_credentials', sa.Boolean(), nullable=False),
    sa.Column('health', sa.String(length=32), nullable=False),
    sa.Column('health_reason', sa.Text(), nullable=True),
    sa.Column('circuit_state', sa.String(length=16), nullable=False),
    sa.Column('circuit_opened_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.Column('consecutive_successes', sa.Integer(), nullable=False),
    sa.Column('consecutive_empty_runs', sa.Integer(), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error_type', sa.String(length=64), nullable=True),
    sa.Column('last_error_message', sa.Text(), nullable=True),
    sa.Column('last_duration_seconds', sa.Float(), nullable=True),
    sa.Column('last_item_count', sa.Integer(), nullable=False),
    sa.Column('total_runs', sa.Integer(), nullable=False),
    sa.Column('total_failures', sa.Integer(), nullable=False),
    sa.Column('total_items_found', sa.Integer(), nullable=False),
    sa.Column('total_items_ingested', sa.Integer(), nullable=False),
    sa.Column('total_duplicates', sa.Integer(), nullable=False),
    sa.Column('config_checksum', sa.String(length=64), nullable=True),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sources')),
    comment='One row per connector; holds runtime health and breaker state.'
    )
    op.create_index(op.f('ix_sources_country'), 'sources', ['country'], unique=False)
    op.create_index(op.f('ix_sources_created_at'), 'sources', ['created_at'], unique=False)
    op.create_index(op.f('ix_sources_health'), 'sources', ['health'], unique=False)
    op.create_index('ix_sources_health_enabled', 'sources', ['health', 'enabled'], unique=False)
    op.create_index(op.f('ix_sources_key'), 'sources', ['key'], unique=True)
    op.create_index(op.f('ix_sources_last_run_at'), 'sources', ['last_run_at'], unique=False)
    op.create_table('user_preferences',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=True),
    sa.Column('display_name', sa.String(length=255), nullable=True),
    sa.Column('team', sa.String(length=128), nullable=True),
    sa.Column('company', sa.String(length=128), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('sectors', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('industries', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('countries', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('keywords', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('connectors', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('buyers', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('cpv_codes', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('excluded_keywords', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('channels', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('min_relevance_band', sa.String(length=32), nullable=False),
    sa.Column('min_score', sa.Float(), nullable=True),
    sa.Column('min_budget', sa.Float(), nullable=True),
    sa.Column('digest_frequency', sa.String(length=16), nullable=False),
    sa.Column('max_notifications_per_day', sa.Integer(), nullable=False),
    sa.Column('quiet_hours_start', sa.Integer(), nullable=True),
    sa.Column('quiet_hours_end', sa.Integer(), nullable=True),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_preferences')),
    comment='Per-user notification targeting rules.'
    )
    op.create_index('ix_user_preferences_active', 'user_preferences', ['active'], unique=False)
    op.create_index(op.f('ix_user_preferences_company'), 'user_preferences', ['company'], unique=False)
    op.create_index(op.f('ix_user_preferences_created_at'), 'user_preferences', ['created_at'], unique=False)
    op.create_index(op.f('ix_user_preferences_team'), 'user_preferences', ['team'], unique=False)
    op.create_index(op.f('ix_user_preferences_user_id'), 'user_preferences', ['user_id'], unique=True)
    op.create_table('scraping_jobs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('trigger', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('requested_connectors', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('filters', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('requested_by', sa.String(length=128), nullable=True),
    sa.Column('schedule_id', sa.Uuid(), nullable=True),
    sa.Column('celery_task_id', sa.String(length=128), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_seconds', sa.Float(), nullable=True),
    sa.Column('connectors_total', sa.Integer(), nullable=False),
    sa.Column('connectors_succeeded', sa.Integer(), nullable=False),
    sa.Column('connectors_failed', sa.Integer(), nullable=False),
    sa.Column('connectors_skipped', sa.Integer(), nullable=False),
    sa.Column('items_found', sa.Integer(), nullable=False),
    sa.Column('items_ingested', sa.Integer(), nullable=False),
    sa.Column('items_duplicate', sa.Integer(), nullable=False),
    sa.Column('items_rejected', sa.Integer(), nullable=False),
    sa.Column('errors', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id'], name=op.f('fk_scraping_jobs_schedule_id_schedules'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scraping_jobs')),
    comment='One user- or schedule-initiated scraping request.'
    )
    op.create_index(op.f('ix_scraping_jobs_celery_task_id'), 'scraping_jobs', ['celery_task_id'], unique=False)
    op.create_index(op.f('ix_scraping_jobs_created_at'), 'scraping_jobs', ['created_at'], unique=False)
    op.create_index(op.f('ix_scraping_jobs_requested_by'), 'scraping_jobs', ['requested_by'], unique=False)
    op.create_index(op.f('ix_scraping_jobs_schedule_id'), 'scraping_jobs', ['schedule_id'], unique=False)
    op.create_index(op.f('ix_scraping_jobs_status'), 'scraping_jobs', ['status'], unique=False)
    op.create_index('ix_scraping_jobs_status_created', 'scraping_jobs', ['status', 'created_at'], unique=False)
    op.create_index(op.f('ix_scraping_jobs_trigger'), 'scraping_jobs', ['trigger'], unique=False)
    op.create_index('ix_scraping_jobs_trigger_created', 'scraping_jobs', ['trigger', 'created_at'], unique=False)
    op.create_table('tenders',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=True),
    sa.Column('source_key', sa.String(length=64), nullable=False),
    sa.Column('entry_point', sa.String(length=32), nullable=False),
    sa.Column('source_url', sa.String(length=1024), nullable=True),
    sa.Column('canonical_url', sa.String(length=1024), nullable=True),
    sa.Column('external_id', sa.String(length=255), nullable=True),
    sa.Column('reference', sa.String(length=255), nullable=True),
    sa.Column('title', sa.String(length=1024), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('buyer', sa.String(length=512), nullable=True),
    sa.Column('funding_organization', sa.String(length=512), nullable=True),
    sa.Column('contact_email', sa.String(length=255), nullable=True),
    sa.Column('language', sa.String(length=16), nullable=True),
    sa.Column('country', sa.String(length=128), nullable=True),
    sa.Column('location', sa.String(length=255), nullable=True),
    sa.Column('sector', sa.String(length=255), nullable=True),
    sa.Column('category', sa.String(length=255), nullable=True),
    sa.Column('cpv_codes', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('procurement_type', sa.String(length=48), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('publication_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('estimated_budget', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=8), nullable=True),
    sa.Column('budget_reference_amount', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('raw_sha256', sa.String(length=64), nullable=True),
    sa.Column('text_sha256', sa.String(length=64), nullable=True),
    sa.Column('dedup_vector', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=True),
    sa.Column('storage_bucket', sa.String(length=128), nullable=True),
    sa.Column('storage_key', sa.String(length=1024), nullable=True),
    sa.Column('original_filename', sa.String(length=512), nullable=True),
    sa.Column('content_type', sa.String(length=128), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('extracted_text', sa.Text(), nullable=True),
    sa.Column('extraction_status', sa.String(length=24), nullable=False),
    sa.Column('extraction_method', sa.String(length=16), nullable=True),
    sa.Column('extraction_chars', sa.Integer(), nullable=False),
    sa.Column('extraction_error', sa.Text(), nullable=True),
    sa.Column('pipeline_state', sa.String(length=32), nullable=False),
    sa.Column('pipeline_error', sa.Text(), nullable=True),
    sa.Column('duplicate_hits', sa.Integer(), nullable=False),
    sa.Column('seen_on_sources', postgresql.ARRAY(sa.String()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('relevance_score', sa.Float(), nullable=True),
    sa.Column('relevance_band', sa.String(length=32), nullable=False),
    sa.Column('score_profile_version', sa.String(length=32), nullable=True),
    sa.Column('scored_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_tenders_source_id_sources'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tenders')),
    sa.UniqueConstraint('canonical_url', name='uq_tenders_canonical_url'),
    comment='Canonical tender record; PK is the platform-wide master UUID.'
    )
    op.create_index('ix_tenders_band_deadline', 'tenders', ['relevance_band', 'deadline'], unique=False)
    op.create_index(op.f('ix_tenders_buyer'), 'tenders', ['buyer'], unique=False)
    op.create_index(op.f('ix_tenders_country'), 'tenders', ['country'], unique=False)
    op.create_index('ix_tenders_country_sector', 'tenders', ['country', 'sector'], unique=False)
    op.create_index(op.f('ix_tenders_created_at'), 'tenders', ['created_at'], unique=False)
    op.create_index(op.f('ix_tenders_deadline'), 'tenders', ['deadline'], unique=False)
    op.create_index(op.f('ix_tenders_entry_point'), 'tenders', ['entry_point'], unique=False)
    op.create_index(op.f('ix_tenders_ingested_at'), 'tenders', ['ingested_at'], unique=False)
    op.create_index(op.f('ix_tenders_pipeline_state'), 'tenders', ['pipeline_state'], unique=False)
    op.create_index(op.f('ix_tenders_publication_date'), 'tenders', ['publication_date'], unique=False)
    op.create_index('ix_tenders_raw_sha256', 'tenders', ['raw_sha256'], unique=False)
    op.create_index(op.f('ix_tenders_reference'), 'tenders', ['reference'], unique=False)
    op.create_index(op.f('ix_tenders_relevance_band'), 'tenders', ['relevance_band'], unique=False)
    op.create_index(op.f('ix_tenders_relevance_score'), 'tenders', ['relevance_score'], unique=False)
    op.create_index(op.f('ix_tenders_sector'), 'tenders', ['sector'], unique=False)
    op.create_index('ix_tenders_source_external', 'tenders', ['source_key', 'external_id'], unique=False)
    op.create_index(op.f('ix_tenders_source_id'), 'tenders', ['source_id'], unique=False)
    op.create_index(op.f('ix_tenders_source_key'), 'tenders', ['source_key'], unique=False)
    op.create_index('ix_tenders_state_created', 'tenders', ['pipeline_state', 'created_at'], unique=False)
    op.create_index(op.f('ix_tenders_status'), 'tenders', ['status'], unique=False)
    op.create_index('ix_tenders_text_sha256', 'tenders', ['text_sha256'], unique=False)
    op.create_table('connector_runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=True),
    sa.Column('connector_key', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('celery_task_id', sa.String(length=128), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_seconds', sa.Float(), nullable=True),
    sa.Column('pages_fetched', sa.Integer(), nullable=False),
    sa.Column('http_requests', sa.Integer(), nullable=False),
    sa.Column('http_retries', sa.Integer(), nullable=False),
    sa.Column('bytes_downloaded', sa.Integer(), nullable=False),
    sa.Column('items_found', sa.Integer(), nullable=False),
    sa.Column('items_ingested', sa.Integer(), nullable=False),
    sa.Column('items_duplicate', sa.Integer(), nullable=False),
    sa.Column('items_rejected', sa.Integer(), nullable=False),
    sa.Column('items_failed', sa.Integer(), nullable=False),
    sa.Column('error_type', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('error_context', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('item_errors', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('filters', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['scraping_jobs.id'], name=op.f('fk_connector_runs_job_id_scraping_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], name=op.f('fk_connector_runs_source_id_sources'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_connector_runs')),
    comment="One connector's isolated attempt within a scraping job."
    )
    op.create_index(op.f('ix_connector_runs_celery_task_id'), 'connector_runs', ['celery_task_id'], unique=False)
    op.create_index(op.f('ix_connector_runs_connector_key'), 'connector_runs', ['connector_key'], unique=False)
    op.create_index(op.f('ix_connector_runs_created_at'), 'connector_runs', ['created_at'], unique=False)
    op.create_index(op.f('ix_connector_runs_error_type'), 'connector_runs', ['error_type'], unique=False)
    op.create_index('ix_connector_runs_source_created', 'connector_runs', ['source_id', 'created_at'], unique=False)
    op.create_index('ix_connector_runs_status', 'connector_runs', ['status'], unique=False)
    op.create_table('duplicate_records',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('canonical_tender_id', sa.Uuid(), nullable=True),
    sa.Column('strategy', sa.String(length=32), nullable=False),
    sa.Column('similarity', sa.Float(), nullable=True),
    sa.Column('source_key', sa.String(length=64), nullable=True),
    sa.Column('source_url', sa.String(length=1024), nullable=True),
    sa.Column('canonical_url', sa.String(length=1024), nullable=True),
    sa.Column('raw_sha256', sa.String(length=64), nullable=True),
    sa.Column('text_sha256', sa.String(length=64), nullable=True),
    sa.Column('title', sa.String(length=1024), nullable=True),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['canonical_tender_id'], ['tenders.id'], name=op.f('fk_duplicate_records_canonical_tender_id_tenders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_duplicate_records')),
    comment='Incoming records rejected as duplicates, with evidence.'
    )
    op.create_index('ix_duplicate_records_canonical', 'duplicate_records', ['canonical_tender_id'], unique=False)
    op.create_index(op.f('ix_duplicate_records_created_at'), 'duplicate_records', ['created_at'], unique=False)
    op.create_index(op.f('ix_duplicate_records_source_key'), 'duplicate_records', ['source_key'], unique=False)
    op.create_index(op.f('ix_duplicate_records_strategy'), 'duplicate_records', ['strategy'], unique=False)
    op.create_index('ix_duplicate_records_strategy_created', 'duplicate_records', ['strategy', 'created_at'], unique=False)
    op.create_table('notifications',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.String(length=128), nullable=False),
    sa.Column('tender_id', sa.Uuid(), nullable=True),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('subject', sa.String(length=512), nullable=True),
    sa.Column('body', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('match_reason', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tender_id'], ['tenders.id'], name=op.f('fk_notifications_tender_id_tenders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications')),
    comment='One delivery attempt of one tender to one user on one channel.'
    )
    op.create_index(op.f('ix_notifications_created_at'), 'notifications', ['created_at'], unique=False)
    op.create_index('ix_notifications_dedup', 'notifications', ['user_id', 'tender_id', 'channel'], unique=True)
    op.create_index(op.f('ix_notifications_status'), 'notifications', ['status'], unique=False)
    op.create_index('ix_notifications_tender', 'notifications', ['tender_id'], unique=False)
    op.create_index(op.f('ix_notifications_user_id'), 'notifications', ['user_id'], unique=False)
    op.create_index('ix_notifications_user_status', 'notifications', ['user_id', 'status'], unique=False)
    op.create_table('submissions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tender_id', sa.Uuid(), nullable=True),
    sa.Column('buyer', sa.String(length=512), nullable=True),
    sa.Column('sector', sa.String(length=255), nullable=True),
    sa.Column('country', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('bid_amount', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=8), nullable=True),
    sa.Column('winning_amount', sa.Numeric(precision=18, scale=2), nullable=True),
    sa.Column('winner_name', sa.String(length=512), nullable=True),
    sa.Column('outcome_reason', sa.Text(), nullable=True),
    sa.Column('owner', sa.String(length=128), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tender_id'], ['tenders.id'], name=op.f('fk_submissions_tender_id_tenders'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_submissions')),
    comment='Our bid on a tender, and how it turned out.'
    )
    op.create_index(op.f('ix_submissions_buyer'), 'submissions', ['buyer'], unique=False)
    op.create_index('ix_submissions_buyer_outcome', 'submissions', ['buyer', 'outcome'], unique=False)
    op.create_index(op.f('ix_submissions_created_at'), 'submissions', ['created_at'], unique=False)
    op.create_index(op.f('ix_submissions_outcome'), 'submissions', ['outcome'], unique=False)
    op.create_index(op.f('ix_submissions_owner'), 'submissions', ['owner'], unique=False)
    op.create_index(op.f('ix_submissions_sector'), 'submissions', ['sector'], unique=False)
    op.create_index('ix_submissions_sector_outcome', 'submissions', ['sector', 'outcome'], unique=False)
    op.create_index(op.f('ix_submissions_status'), 'submissions', ['status'], unique=False)
    op.create_index('ix_submissions_tender', 'submissions', ['tender_id'], unique=False)
    op.create_index(op.f('ix_submissions_tender_id'), 'submissions', ['tender_id'], unique=False)
    op.create_table('tender_documents',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tender_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=512), nullable=True),
    sa.Column('source_url', sa.String(length=1024), nullable=True),
    sa.Column('storage_bucket', sa.String(length=128), nullable=True),
    sa.Column('storage_key', sa.String(length=1024), nullable=True),
    sa.Column('content_type', sa.String(length=128), nullable=True),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('downloaded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tender_id'], ['tenders.id'], name=op.f('fk_tender_documents_tender_id_tenders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tender_documents')),
    sa.UniqueConstraint('tender_id', 'sha256', name='uq_tender_documents_tender_sha'),
    comment='Attachments downloaded alongside a tender notice.'
    )
    op.create_index(op.f('ix_tender_documents_created_at'), 'tender_documents', ['created_at'], unique=False)
    op.create_index('ix_tender_documents_tender', 'tender_documents', ['tender_id'], unique=False)
    op.create_table('tender_scores',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tender_id', sa.Uuid(), nullable=False),
    sa.Column('profile_name', sa.String(length=64), nullable=False),
    sa.Column('profile_version', sa.String(length=32), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('band', sa.String(length=32), nullable=False),
    sa.Column('breakdown', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('weights', postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite'), nullable=False),
    sa.Column('duration_ms', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['tender_id'], ['tenders.id'], name=op.f('fk_tender_scores_tender_id_tenders'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tender_scores')),
    comment='Immutable history of scoring executions.'
    )
    op.create_index(op.f('ix_tender_scores_created_at'), 'tender_scores', ['created_at'], unique=False)
    op.create_index(op.f('ix_tender_scores_profile_version'), 'tender_scores', ['profile_version'], unique=False)
    op.create_index('ix_tender_scores_tender_created', 'tender_scores', ['tender_id', 'created_at'], unique=False)

    _postgres_extras()

    # Seed the Beat change sentinel so the scheduler has a row to poll from the
    # very first tick.
    op.execute(
        "INSERT INTO schedule_change_sentinel (id, last_update) "
        "VALUES (1, CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING"
        if op.get_bind().dialect.name == "postgresql"
        else "INSERT OR IGNORE INTO schedule_change_sentinel (id, last_update) "
        "VALUES (1, CURRENT_TIMESTAMP)"
    )


def _postgres_extras() -> None:
    """Indexes and seed rows that autogenerate cannot infer.

    Trigram indexes back the dashboard's ``ILIKE '%term%'`` search, which a
    B-tree cannot serve at all. The full-text index makes the extracted
    document text searchable without scanning a column that holds hundreds of
    kilobytes per row.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_title_trgm "
        "ON tenders USING gin (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_buyer_trgm "
        "ON tenders USING gin (buyer gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_cpv_gin ON tenders USING gin (cpv_codes)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tenders_extracted_text_fts "
        "ON tenders USING gin (to_tsvector('french', coalesce(extracted_text, '')))"
    )


def downgrade() -> None:
    op.drop_index('ix_tender_scores_tender_created', table_name='tender_scores')
    op.drop_index(op.f('ix_tender_scores_profile_version'), table_name='tender_scores')
    op.drop_index(op.f('ix_tender_scores_created_at'), table_name='tender_scores')
    op.drop_table('tender_scores')
    op.drop_index('ix_tender_documents_tender', table_name='tender_documents')
    op.drop_index(op.f('ix_tender_documents_created_at'), table_name='tender_documents')
    op.drop_table('tender_documents')
    op.drop_index(op.f('ix_submissions_tender_id'), table_name='submissions')
    op.drop_index('ix_submissions_tender', table_name='submissions')
    op.drop_index(op.f('ix_submissions_status'), table_name='submissions')
    op.drop_index('ix_submissions_sector_outcome', table_name='submissions')
    op.drop_index(op.f('ix_submissions_sector'), table_name='submissions')
    op.drop_index(op.f('ix_submissions_owner'), table_name='submissions')
    op.drop_index(op.f('ix_submissions_outcome'), table_name='submissions')
    op.drop_index(op.f('ix_submissions_created_at'), table_name='submissions')
    op.drop_index('ix_submissions_buyer_outcome', table_name='submissions')
    op.drop_index(op.f('ix_submissions_buyer'), table_name='submissions')
    op.drop_table('submissions')
    op.drop_index('ix_notifications_user_status', table_name='notifications')
    op.drop_index(op.f('ix_notifications_user_id'), table_name='notifications')
    op.drop_index('ix_notifications_tender', table_name='notifications')
    op.drop_index(op.f('ix_notifications_status'), table_name='notifications')
    op.drop_index('ix_notifications_dedup', table_name='notifications')
    op.drop_index(op.f('ix_notifications_created_at'), table_name='notifications')
    op.drop_table('notifications')
    op.drop_index('ix_duplicate_records_strategy_created', table_name='duplicate_records')
    op.drop_index(op.f('ix_duplicate_records_strategy'), table_name='duplicate_records')
    op.drop_index(op.f('ix_duplicate_records_source_key'), table_name='duplicate_records')
    op.drop_index(op.f('ix_duplicate_records_created_at'), table_name='duplicate_records')
    op.drop_index('ix_duplicate_records_canonical', table_name='duplicate_records')
    op.drop_table('duplicate_records')
    op.drop_index('ix_connector_runs_status', table_name='connector_runs')
    op.drop_index('ix_connector_runs_source_created', table_name='connector_runs')
    op.drop_index(op.f('ix_connector_runs_error_type'), table_name='connector_runs')
    op.drop_index(op.f('ix_connector_runs_created_at'), table_name='connector_runs')
    op.drop_index(op.f('ix_connector_runs_connector_key'), table_name='connector_runs')
    op.drop_index(op.f('ix_connector_runs_celery_task_id'), table_name='connector_runs')
    op.drop_table('connector_runs')
    op.drop_index('ix_tenders_text_sha256', table_name='tenders')
    op.drop_index(op.f('ix_tenders_status'), table_name='tenders')
    op.drop_index('ix_tenders_state_created', table_name='tenders')
    op.drop_index(op.f('ix_tenders_source_key'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_source_id'), table_name='tenders')
    op.drop_index('ix_tenders_source_external', table_name='tenders')
    op.drop_index(op.f('ix_tenders_sector'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_relevance_score'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_relevance_band'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_reference'), table_name='tenders')
    op.drop_index('ix_tenders_raw_sha256', table_name='tenders')
    op.drop_index(op.f('ix_tenders_publication_date'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_pipeline_state'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_ingested_at'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_entry_point'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_deadline'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_created_at'), table_name='tenders')
    op.drop_index('ix_tenders_country_sector', table_name='tenders')
    op.drop_index(op.f('ix_tenders_country'), table_name='tenders')
    op.drop_index(op.f('ix_tenders_buyer'), table_name='tenders')
    op.drop_index('ix_tenders_band_deadline', table_name='tenders')
    op.drop_table('tenders')
    op.drop_index('ix_scraping_jobs_trigger_created', table_name='scraping_jobs')
    op.drop_index(op.f('ix_scraping_jobs_trigger'), table_name='scraping_jobs')
    op.drop_index('ix_scraping_jobs_status_created', table_name='scraping_jobs')
    op.drop_index(op.f('ix_scraping_jobs_status'), table_name='scraping_jobs')
    op.drop_index(op.f('ix_scraping_jobs_schedule_id'), table_name='scraping_jobs')
    op.drop_index(op.f('ix_scraping_jobs_requested_by'), table_name='scraping_jobs')
    op.drop_index(op.f('ix_scraping_jobs_created_at'), table_name='scraping_jobs')
    op.drop_index(op.f('ix_scraping_jobs_celery_task_id'), table_name='scraping_jobs')
    op.drop_table('scraping_jobs')
    op.drop_index(op.f('ix_user_preferences_user_id'), table_name='user_preferences')
    op.drop_index(op.f('ix_user_preferences_team'), table_name='user_preferences')
    op.drop_index(op.f('ix_user_preferences_created_at'), table_name='user_preferences')
    op.drop_index(op.f('ix_user_preferences_company'), table_name='user_preferences')
    op.drop_index('ix_user_preferences_active', table_name='user_preferences')
    op.drop_table('user_preferences')
    op.drop_index(op.f('ix_sources_last_run_at'), table_name='sources')
    op.drop_index(op.f('ix_sources_key'), table_name='sources')
    op.drop_index('ix_sources_health_enabled', table_name='sources')
    op.drop_index(op.f('ix_sources_health'), table_name='sources')
    op.drop_index(op.f('ix_sources_created_at'), table_name='sources')
    op.drop_index(op.f('ix_sources_country'), table_name='sources')
    op.drop_table('sources')
    op.drop_index(op.f('ix_schedules_next_run_at'), table_name='schedules')
    op.drop_index('ix_schedules_enabled_next_run', table_name='schedules')
    op.drop_index(op.f('ix_schedules_enabled'), table_name='schedules')
    op.drop_index(op.f('ix_schedules_created_at'), table_name='schedules')
    op.drop_table('schedules')
    op.drop_table('schedule_change_sentinel')
    op.drop_index('ix_execution_logs_ts', table_name='execution_logs')
    op.drop_index('ix_execution_logs_tender_ts', table_name='execution_logs')
    op.drop_index(op.f('ix_execution_logs_stage'), table_name='execution_logs')
    op.drop_index('ix_execution_logs_level_ts', table_name='execution_logs')
    op.drop_index('ix_execution_logs_job_ts', table_name='execution_logs')
    op.drop_index(op.f('ix_execution_logs_error_type'), table_name='execution_logs')
    op.drop_index(op.f('ix_execution_logs_correlation_id'), table_name='execution_logs')
    op.drop_index('ix_execution_logs_connector_event', table_name='execution_logs')
    op.drop_table('execution_logs')
