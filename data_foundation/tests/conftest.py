from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
from uuid import uuid4

import pytest

from data_foundation import AssetCreate, EntityCreate, SourceCreate, create_store
from data_foundation.db import Base
from data_foundation.seed import seed_catalog


SCENIC_ID = "00000000-0000-0000-0000-000000000101"
CAMERA_ID = "00000000-0000-0000-0000-000000000102"
SOURCE_ID = "00000000-0000-0000-0000-000000000201"


@pytest.fixture()
def store(tmp_path: Path):
    database = tmp_path / "test.sqlite"
    mysql_admin = os.environ.get("COVERAGE_MYSQL_ADMIN_URL")
    if mysql_admin:
        from sqlalchemy import create_engine, inspect, text
        from sqlalchemy.engine import make_url
        parsed = make_url(mysql_admin)
        if parsed.drivername != "mysql+pymysql" or parsed.database != "c018_schema_anchor_test":
            raise ValueError("只允许本轮专用空schema anchor测试库")
        admin_engine = create_engine(mysql_admin)
        try:
            if inspect(admin_engine).get_table_names():
                raise ValueError("schema anchor必须为空；不删除已有数据")
            name = "c018_case_"+uuid4().hex[:16]+"_test"
            with admin_engine.connect() as connection:
                connection.execute(text("CREATE DATABASE "+name+" CHARACTER SET utf8mb4"))
        finally:
            admin_engine.dispose()
        value = create_store(parsed.set(database=name).render_as_string(hide_password=False))
    else:
        value = create_store(f"sqlite+pysqlite:///{database}", allow_sqlite_for_tests=True)
    Base.metadata.create_all(value.engine)
    seed_catalog(value)
    with value.write() as uow:
        uow.catalog.create_entity(EntityCreate(
            id=SCENIC_ID, name="测试景区", type="scenic", region_code="340000"
        ))
        uow.catalog.create_entity(EntityCreate(
            id=CAMERA_ID, name="测试景区东门摄像头", type="camera", region_code="340000",
            parent_entity_id=SCENIC_ID,
        ))
        uow.provenance.register_source(SourceCreate(
            id=SOURCE_ID, publisher="测试发布单位", title="测试来源（仅测试库）",
            url="https://example.invalid/source", usage_note="仅用于自动化测试",
            update_frequency="monthly", native_grain="month", verification_status="verified",
        ))
    yield value
    value.dispose()


def register_asset(store, *, sha: str = "a" * 64, suffix: str = "v1") -> str:
    with store.write() as uow:
        result = uow.provenance.register_asset(AssetCreate(
            source_id=SOURCE_ID,
            url=f"https://example.invalid/source/{suffix}",
            title=f"测试原件 {suffix}",
            retrieved_at=datetime.now(timezone.utc),
            relative_path=f"raw/test/{suffix}.csv",
            sha256=sha,
            media_type="text/csv",
            byte_size=100,
            usage_note="仅用于自动化测试",
            published_at=datetime.now(timezone.utc),
        ))
        return result["id"]
