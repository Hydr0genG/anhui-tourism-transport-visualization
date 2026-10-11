"""Preserve microseconds on MySQL for all existing datetime columns.

Revision ID: 0005
Revises: 0004
SQLite already preserves microseconds and needs no schema alteration.
Downgrade to DATETIME(0) can round away precision; never use it for normal rollback.
"""
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Frozen at revision 0004; do not derive historical migrations from live models.
TIME_COLUMNS = (
    ('entities', 'updated_at', False),
    ('entities', 'created_at', False),
    ('metrics', 'created_at', False),
    ('sources', 'published_at', True),
    ('sources', 'retrieved_at', True),
    ('sources', 'last_checked_at', True),
    ('sources', 'updated_at', False),
    ('sources', 'created_at', False),
    ('users', 'updated_at', False),
    ('users', 'created_at', False),
    ('forecast_runs', 'created_at', False),
    ('jobs', 'available_at', False),
    ('jobs', 'started_at', True),
    ('jobs', 'finished_at', True),
    ('jobs', 'created_at', False),
    ('permission_records', 'valid_from', False),
    ('permission_records', 'valid_until', True),
    ('permission_records', 'updated_at', False),
    ('permission_records', 'created_at', False),
    ('series_policies', 'created_at', False),
    ('sessions', 'expires_at', False),
    ('sessions', 'revoked_at', True),
    ('sessions', 'created_at', False),
    ('source_assets', 'published_at', True),
    ('source_assets', 'retrieved_at', False),
    ('source_assets', 'created_at', False),
    ('events', 'starts_at', False),
    ('events', 'ends_at', True),
    ('events', 'published_at', True),
    ('events', 'last_verified_at', True),
    ('events', 'updated_at', False),
    ('events', 'created_at', False),
    ('forecast_points', 'created_at', False),
    ('holidays', 'updated_at', False),
    ('holidays', 'created_at', False),
    ('job_attempts', 'lease_expires_at', False),
    ('job_attempts', 'heartbeat_at', False),
    ('job_attempts', 'started_at', False),
    ('job_attempts', 'finished_at', True),
    ('job_attempts', 'created_at', False),
    ('knowledge_documents', 'published_at', False),
    ('knowledge_documents', 'valid_from', True),
    ('knowledge_documents', 'valid_until', True),
    ('knowledge_documents', 'updated_at', False),
    ('knowledge_documents', 'created_at', False),
    ('videos', 'recorded_start_at', False),
    ('videos', 'recorded_end_at', False),
    ('videos', 'updated_at', False),
    ('videos', 'created_at', False),
    ('event_history', 'changed_at', False),
    ('event_history', 'created_at', False),
    ('ingestion_runs', 'started_at', False),
    ('ingestion_runs', 'finished_at', True),
    ('ingestion_runs', 'created_at', False),
    ('knowledge_indexes', 'created_at', False),
    ('manual_annotations', 'window_start', False),
    ('manual_annotations', 'window_end', False),
    ('manual_annotations', 'created_at', False),
    ('video_analyses', 'created_at', False),
    ('camera_counts', 'window_start', False),
    ('camera_counts', 'window_end', False),
    ('camera_counts', 'created_at', False),
    ('knowledge_chunks', 'created_at', False),
    ('observations', 'published_at', True),
    ('observations', 'ingested_at', False),
    ('observations', 'updated_at', False),
    ('observations', 'created_at', False),
    ('short_forecast_runs', 'cutoff_at', False),
    ('short_forecast_runs', 'created_at', False),
    ('observation_revisions', 'changed_at', False),
    ('observation_revisions', 'created_at', False),
    ('quality_issues', 'resolved_at', True),
    ('quality_issues', 'updated_at', False),
    ('quality_issues', 'created_at', False),
    ('short_forecast_points', 'window_start', False),
    ('short_forecast_points', 'window_end', False),
    ('short_forecast_points', 'created_at', False),
)


def _change_precision(old: int, new: int) -> None:
    if op.get_context().dialect.name not in {"mysql", "mariadb"}:
        return
    for table, column, nullable in TIME_COLUMNS:
        op.alter_column(table, column, existing_type=DATETIME(fsp=old),
                        type_=DATETIME(fsp=new), existing_nullable=nullable)


def upgrade() -> None:
    _change_precision(0, 6)


def downgrade() -> None:
    _change_precision(6, 0)
