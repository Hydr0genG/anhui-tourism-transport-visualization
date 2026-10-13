from pathlib import Path
import importlib.util

from sqlalchemy import DateTime
from sqlalchemy.dialects import mysql, sqlite
from alembic import command
from alembic.config import Config

from data_foundation.db import Base


def test_every_model_datetime_and_migration_preserve_mysql_microseconds(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("precision_revision", root / "alembic/versions/0005_datetime_precision.py")
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    actual = set()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime):
                assert column.type.compile(dialect=mysql.dialect()) == "DATETIME(6)"
                assert column.type.compile(dialect=sqlite.dialect()) == "DATETIME"
                actual.add((table.name, column.name, column.nullable))
    assert actual == set(revision.TIME_COLUMNS) | {
        ("job_retries", "created_at", False), ("job_retries", "requested_at", False)
    }
    # Compile the upgrade against MySQL without claiming it was executed on a server.
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://test:test@localhost/precision_test")
    output = tmp_path / "upgrade.sql"
    with output.open("w") as stream:
        config = Config(str(root / "alembic.ini"), output_buffer=stream)
        command.upgrade(config, "0004:0005", sql=True)
    sql = output.read_text()
    assert sql.count("DATETIME(6)") == len(revision.TIME_COLUMNS)
    assert sql.count("ALTER TABLE") == len(revision.TIME_COLUMNS)


def test_precision_upgrade_keeps_existing_sqlite_rows(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    root = Path(__file__).resolve().parents[1]
    url = f"sqlite+pysqlite:///{tmp_path / 'upgrade.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(root / "alembic.ini"))
    command.upgrade(config, "0004")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id,username,display_name,password_hash,role,status,created_at,updated_at,row_version) VALUES ('precision-user','precision-user','test','test','viewer','active','2026-10-11 08:14:07.700000','2026-10-11 08:14:07.700000',1)"))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT created_at FROM users WHERE id='precision-user'")).scalar_one() == "2026-10-11 08:14:07.700000"
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0006"
    engine.dispose()


def test_same_time_claim_and_session_lease_expiry(store):
    # Reuse the receiving-party check so both environments test the exact bug and boundaries.
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("handoff_precision", root / "examples/de_handoff_smoke.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    checks = []
    demo.datetime_precision_checks(store, lambda name, note: checks.append(name))
    assert checks == ["precision-job", "precision-lease", "precision-session"]
