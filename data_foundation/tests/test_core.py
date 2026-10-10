from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from data_foundation import SeriesQuery, SessionCreate, UserCreate
from data_foundation.db.models import ObservationRevision
from data_foundation.ingestion import import_standard_csv

from conftest import SCENIC_ID, SOURCE_ID, register_asset


def _write_csv(path: Path, value: str) -> None:
    path.write_text(
        "entity_id,metric_code,period_start,period_end,grain,original_value,original_unit,"
        "standard_value,standard_unit,conversion_factor,source_locator,methodology_version,"
        "published_at,quality_status\n"
        f"{SCENIC_ID},scenic_visits,2026-01-01,2026-01-31,month,{value},人次,"
        f"{value},人次,1,测试表第2行,test-method-v1,2026-02-10T09:00:00+08:00,verified\n",
        encoding="utf-8",
    )


def test_auth_session_lifecycle(store):
    now = datetime.now(timezone.utc)
    with store.write() as uow:
        user = uow.auth.create_user(UserCreate("viewer01", "测试用户", "argon2id-test-hash"))
        uow.auth.save_session(SessionCreate("b" * 64, user["id"], now + timedelta(hours=1)), now)
    with store.read() as uow:
        assert uow.auth.get_active_session("b" * 64, now)["user"]["username"] == "viewer01"
    with store.write() as uow:
        assert uow.auth.revoke_session("b" * 64, now) is True
        assert uow.auth.revoke_session("b" * 64, now) is False
    with store.read() as uow:
        assert uow.auth.get_active_session("b" * 64, now) is None


def test_import_is_idempotent_and_revision_is_traceable(store, tmp_path):
    csv_path = tmp_path / "data.csv"
    first_asset = register_asset(store)
    _write_csv(csv_path, "125000")
    first = import_standard_csv(store, csv_path, source_id=SOURCE_ID, asset_id=first_asset, dry_run=False)
    assert first["counts"] == {"processed": 1, "imported": 1, "revised": 0, "skipped": 0, "quarantined": 0}

    second = import_standard_csv(store, csv_path, source_id=SOURCE_ID, asset_id=first_asset, dry_run=False)
    assert second["counts"]["skipped"] == 1

    second_asset = register_asset(store, sha="c" * 64, suffix="v2")
    _write_csv(csv_path, "126000")
    revised = import_standard_csv(store, csv_path, source_id=SOURCE_ID, asset_id=second_asset, dry_run=False)
    assert revised["counts"]["revised"] == 1

    with store.read() as uow:
        series = uow.observations.query_series(SeriesQuery(
            entity_id=SCENIC_ID, metric="scenic_visits", grain="month",
            start=date(2026, 1, 1), end=date(2026, 3, 31),
        ))
        assert series["points"][0]["value"] == Decimal("126000")
        assert series["coverage_ratio"] == Decimal(1) / Decimal(3)
        assert len(series["missing_periods"]) == 2
        revisions = uow.session.query(ObservationRevision).all()
        assert len(revisions) == 1
        assert revisions[0].snapshot["value"] == "125000.0000"
