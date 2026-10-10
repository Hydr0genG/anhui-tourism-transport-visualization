from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import MetaData, engine_from_config, pool

from data_foundation.db import Base


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)


MIGRATION_GROUPS = {
    1: {"users", "sessions", "entities", "metrics", "sources", "jobs", "job_attempts"},
    2: {
        "source_assets", "ingestion_runs", "observations", "observation_revisions",
        "series_policies", "quality_issues",
    },
    3: {
        "holidays", "events", "event_entities", "event_history", "knowledge_documents",
        "document_entities", "knowledge_indexes", "knowledge_chunks",
    },
    4: {
        "permission_records", "videos", "video_analyses", "camera_counts", "manual_annotations",
        "forecast_runs", "forecast_points", "short_forecast_runs", "short_forecast_points",
    },
}


def migration_metadata() -> MetaData:
    stage_text = os.getenv("DATA_FOUNDATION_MIGRATION_STAGE")
    if not stage_text:
        return Base.metadata
    stage = int(stage_text)
    names = set().union(*(MIGRATION_GROUPS[index] for index in range(1, stage + 1)))
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)
    for table in Base.metadata.sorted_tables:
        if table.name in names:
            table.to_metadata(metadata)
    return metadata


target_metadata = migration_metadata()
database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise RuntimeError("请通过环境变量 DATABASE_URL 显式提供数据库连接地址")
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
