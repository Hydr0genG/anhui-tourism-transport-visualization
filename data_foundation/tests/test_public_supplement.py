from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from data_foundation import InvalidArgument
from data_foundation.db.models import Holiday, Event, Observation
from data_foundation.ingestion.real_dataset import import_real_dataset, validate_real_dataset


MANIFEST = Path(__file__).resolve().parents[1] / "real_data/2026-10-12-v2/manifest.json"


def test_public_calendar_and_historical_event_are_queryable_and_idempotent(store, tmp_path):
    store.data_root = tmp_path / "raw"
    result = validate_real_dataset(MANIFEST)
    assert result["holiday_count"] == 26 and result["event_count"] == 1
    first = import_real_dataset(store, MANIFEST)
    assert first["definitions"] == {"holidays": {"inserted": 26, "skipped": 0}, "events": {"inserted": 1, "skipped": 0}}
    second = import_real_dataset(store, MANIFEST)
    assert second["definitions"] == {"holidays": {"inserted": 0, "skipped": 26}, "events": {"inserted": 0, "skipped": 1}}
    assert second["counts"]["imported"] == second["counts"]["revised"] == second["documents"]["inserted"] == 0
    with store.read() as uow:
        new_year = uow.holidays.list_holidays(year=2024, kind="new_year")[0]
        assert new_year["start_date"] == date(2023, 12, 30)
        assert new_year["end_date"] == date(2024, 1, 1)
        assert new_year["days_count"] == 3
        assert len(uow.holidays.list_holidays(year=2025)) == 6
        assert uow.holidays.list_holidays(year=2025, kind="mid_autumn") == []
        combined = uow.holidays.list_holidays(year=2025, kind="national_day")[0]
        assert combined["days_count"] == 8 and "中秋" in combined["name"]
        assert uow.holidays.list_holidays(year=2026, kind="spring_festival")[0]["days_count"] == 9
        assert uow.events.list_events(status="active", now=datetime(2026, 10, 12, tzinfo=timezone.utc)) == []
        assert uow.events.list_events(status="expired")[0]["ends_at"].date() == date(2024, 7, 27)
        assert uow.session.scalar(select(func.count()).select_from(Holiday)) == 26
        assert uow.session.scalar(select(func.count()).select_from(Event)) == 1
        scenic = next(e["id"] for e in json.loads(MANIFEST.read_text())["catalog"]["entities"] if e["type"] == "scenic")
        assert uow.session.scalar(select(func.count()).select_from(Observation).where(Observation.entity_id == scenic)) == 0


@pytest.mark.parametrize("case", ["dates", "combined_duplicate", "workday", "snapshot", "active_event", "document_version"])
def test_invalid_public_definitions_rejected_before_write(store, tmp_path, case):
    root = tmp_path / "package"
    shutil.copytree(MANIFEST.parent, root)
    manifest = json.loads((root/"manifest.json").read_text())
    calendar = json.loads((root/"holiday_calendar.json").read_text())
    if case == "dates":
        calendar["holidays"][0].update(start_date="2022-12-30", days_count=4)
    elif case == "combined_duplicate":
        duplicate = dict(next(h for h in calendar["holidays"] if h["year"] == 2025 and h["kind"] == "national_day"))
        duplicate.update(id="duplicate", kind="mid_autumn")
        calendar["holidays"].append(duplicate)
        manifest["holiday_calendar"]["row_count"] += 1
    elif case == "workday":
        calendar["adjusted_workdays"]["2025"].append("2025-01-28")
    elif case == "snapshot":
        manifest["assets"][-1]["capture_scope"] = "全文原件"
    elif case == "active_event":
        manifest["events"][0]["status"] = "active"
    else:
        manifest["documents"][-1]["document_version"] = "x" * 65
    (root/"holiday_calendar.json").write_text(json.dumps(calendar, ensure_ascii=False))
    manifest["holiday_calendar"]["sha256"] = hashlib.sha256((root/"holiday_calendar.json").read_bytes()).hexdigest()
    (root/"manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    store.data_root = tmp_path/"raw"
    with pytest.raises(InvalidArgument):
        import_real_dataset(store, root/"manifest.json")
    with store.read() as uow:
        assert uow.session.scalar(select(func.count()).select_from(Observation)) == 0
        assert uow.session.scalar(select(func.count()).select_from(Holiday)) == 0


def test_official_holiday_html_body_is_extracted_not_only_navigation(tmp_path):
    import sys
    sys.path.insert(0, str(MANIFEST.parents[2]/"examples"))
    from fetch_real_dataset import Collector
    manifest = json.loads(MANIFEST.read_text())
    asset = dict(next(a for a in manifest["assets"] if a["key"] == "holiday-2026"))
    Collector(tmp_path).extract(asset, (MANIFEST.parent/asset["path"]).read_bytes())
    assert "共9天" in (tmp_path/asset["text_path"]).read_text()
    assert "1月4日（周日）上班" in (tmp_path/asset["text_path"]).read_text()
