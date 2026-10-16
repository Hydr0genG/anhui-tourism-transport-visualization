"""Additive P2PNet storage; do not rewrite any legacy table or business row."""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    precise = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql", "mariadb")
    op.create_table("visible_analyses",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", precise, nullable=False),
        sa.Column("video_id", sa.String(36), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("config_sha256", sa.String(64), nullable=False), sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False), sa.Column("input_identity", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("manual_validation", sa.JSON()), sa.Column("error_summary", sa.Text()))
    op.create_index("ix_visible_analyses_video", "visible_analyses", ["video_id", "created_at"])
    op.create_table("visible_count_points",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", precise, nullable=False),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("visible_analyses.id"), nullable=False),
        sa.Column("point_key", sa.String(64), nullable=False), sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("grain", sa.String(16), nullable=False), sa.Column("recorded_at", precise),
        sa.Column("period_start", precise), sa.Column("period_end", precise), sa.Column("value", sa.Float(53)),
        sa.Column("sample_count", sa.Integer(), nullable=False), sa.Column("quality_status", sa.String(24), nullable=False),
        sa.Column("source_ids", sa.JSON(), nullable=False), sa.Column("published_at", precise), sa.Column("ingested_at", precise),
        sa.UniqueConstraint("analysis_id", "point_key", name="visible_analysis_point"),
        sa.CheckConstraint("sample_count >= 0", name="visible_samples_nonnegative"),
        sa.CheckConstraint("value IS NULL OR value >= 0", name="visible_value_nonnegative"))
    op.create_index("ix_visible_points_time", "visible_count_points", ["analysis_id", "grain", "period_end"])
    op.create_table("visible_forecasts",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", precise, nullable=False),
        sa.Column("analysis_id", sa.String(36), sa.ForeignKey("visible_analyses.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("cutoff_at", precise, nullable=False), sa.Column("horizon_minutes", sa.SmallInteger(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False), sa.Column("input_manifest", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.CheckConstraint("horizon_minutes IN (15,30,60)", name="visible_forecast_horizon"))
    op.create_index("ix_visible_forecast_analysis_cutoff", "visible_forecasts", ["analysis_id", "cutoff_at"])


def downgrade():
    # A data-preserving downgrade must be explicit: never silently drop results.
    if context.is_offline_mode():
        raise RuntimeError("Visible-people downgrade requires an online empty-table check")
    bind = op.get_bind()
    for name in ("visible_forecasts", "visible_count_points", "visible_analyses"):
        if bind.execute(sa.text("SELECT 1 FROM " + name + " LIMIT 1")).first():
            raise RuntimeError("Export and explicitly migrate visible-people data before downgrade")
    op.drop_table("visible_forecasts")
    op.drop_table("visible_count_points")
    op.drop_table("visible_analyses")
