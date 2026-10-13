"""Retry audit and video failure details; preserves all existing business rows."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    precise = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql", "mariadb")
    op.create_table(
        "job_retries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", precise, nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("operator_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_at", precise, nullable=False),
        sa.CheckConstraint("attempt_count >= 0", name="retry_attempt_count_nonnegative"),
    )
    op.create_index("ix_job_retries_job_time", "job_retries", ["job_id", "requested_at"])
    op.add_column("video_analyses", sa.Column("error_summary", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("video_analyses", "error_summary")
    op.drop_index("ix_job_retries_job_time", table_name="job_retries")
    op.drop_table("job_retries")
