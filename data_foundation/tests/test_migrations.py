from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_all_migrations_build_empty_database(tmp_path: Path, monkeypatch):
    database = tmp_path / "migration.sqlite"
    url = f"sqlite+pysqlite:///{database}"
    monkeypatch.setenv("DATABASE_URL", url)
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"users", "observations", "knowledge_chunks", "camera_counts", "forecast_points"} <= tables
    assert len(tables - {"alembic_version"}) == 30
