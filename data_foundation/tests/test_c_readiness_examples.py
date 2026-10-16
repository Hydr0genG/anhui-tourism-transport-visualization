from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from data_foundation import AssetCreate, Conflict, InvalidPermission, ObservationInput, EntityCreate
from conftest import SCENIC_ID, SOURCE_ID


def example(name):
    path = Path(__file__).resolve().parents[1] / "examples" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


metric = example("c_readonly_metric_tool")
inputs = example("c_document_input")
AIRPORT = "00000000-0000-0000-0000-000000000903"


@pytest.fixture
def ready(store):
    with store.write() as uow:
        uow.catalog.create_entity(EntityCreate(AIRPORT, "合成机场", "airport", "340000"))
        asset = uow.provenance.register_asset(AssetCreate(SOURCE_ID, "https://example.invalid/synthetic",
            "仅合成单测", datetime.now(timezone.utc), "synthetic.csv", "a" * 64, "text/csv", 0, "TEST ONLY"))
        run = uow.observations.start_run(source_id=SOURCE_ID, asset_id=asset["id"], dry_run=False, reason="test")
        uow.observations.add_or_revise(ObservationInput(AIRPORT, "airport_throughput", date(2025, 1, 1),
            date(2025, 1, 31), "month", Decimal("1111644"), "人次", "1111644", "人次", Decimal(1),
            SOURCE_ID, asset["id"], "synthetic row", "synthetic-1"), run.id)
    return store


def query(**change):
    return {"metric": "airport_throughput", "entity_type": "station", "entity_ids": [AIRPORT],
            "grain": "month", "period_start": "2025-01-01T00:00:00+08:00",
            "period_end": "2025-02-01T00:00:00+08:00", **change}


def test_native_month_value_sources_and_no_internal_paths(ready):
    tool = metric.build_metric_tool(ready, authorized_entity_ids=[AIRPORT])
    result = tool(query())
    assert result["status"] == "answered" and Decimal(result["value"]) == 1111644
    assert result["source_ids"] == [SOURCE_ID] and result["sample_count"] == 1
    assert result["entity_type"] == "airport" and result["unit"] == "人次"
    assert "asset_id" not in result and "path" not in result


@pytest.mark.parametrize("change", [
    {"metric": []}, {"metric": "camera_visible_people"}, {"metric": "arbitrary_sql"},
    {"grain": []}, {"grain": "year"}, {"grain": "5min"}, {"entity_type": []},
    {"entity_ids": []}, {"entity_ids": [AIRPORT, AIRPORT]}, {"entity_ids": ["not-a-uuid"]},
    {"entity_ids": [SCENIC_ID]}, {"entity_type": "camera"},
    {"period_start": "2025-01-01"}, {"period_start": "2025-01-02T00:00:00+08:00"},
    {"period_end": "2025-01-31T00:00:00+08:00"}, {"period_end": "2025-02-01T01:00:00+08:00"},
    {"period_end": "2025-01-01T00:00:00+08:00"}, {"period_end": "2035-01-01T00:00:00+08:00"},
    {"sql": "select something"},
])
def test_invalid_or_unapproved_query_never_contains_half_success(ready, change):
    result = metric.build_metric_tool(ready, authorized_entity_ids=[AIRPORT])(query(**change))
    assert result["status"] == "metric_tool_unavailable"
    assert "value" not in result and "source_ids" not in result


def test_no_grain_conversion_no_partial_coverage_and_no_empty_allowlist_bypass(ready):
    tool = metric.build_metric_tool(ready, authorized_entity_ids=[AIRPORT])
    assert tool(query(grain="day"))["reason"] == "unsupported_grain"
    assert tool(query(period_end="2025-03-01T00:00:00+08:00"))["reason"] == "data_insufficient"
    assert metric.build_metric_tool(ready, authorized_entity_ids=[])(query())["reason"] == "invalid_entity"


def test_utc_equivalent_bounds_are_the_same_business_month(ready):
    result = metric.build_metric_tool(ready, authorized_entity_ids=[AIRPORT])(query(
        period_start="2024-12-31T16:00:00+00:00", period_end="2025-01-31T16:00:00+00:00"))
    assert result["status"] == "answered" and result["period_start"].endswith("+08:00")


def test_dependency_error_is_sanitized(ready, monkeypatch):
    def failed():
        raise OperationalError("SECRET SQL", {}, Exception("DO_NOT_DISCLOSE"))
    monkeypatch.setattr(ready, "read", failed)
    result = metric.build_metric_tool(ready, authorized_entity_ids=[AIRPORT])(query())
    assert result["reason"] == "data_source_unavailable" and "DO_NOT_DISCLOSE" not in str(result)


def test_original_reference_keeps_registered_version_and_detects_corruption(store, tmp_path):
    store.data_root = tmp_path / "data"
    raw = b"synthetic original, not an index artifact"
    persisted = store.persist_file(source_id=SOURCE_ID, original_name="notice.txt", stream=BytesIO(raw))
    with store.write() as uow:
        asset = uow.provenance.register_asset(AssetCreate(SOURCE_ID, "https://example.invalid/original", "synthetic",
            datetime.now(timezone.utc), persisted["relative_path"], persisted["sha256"], "text/plain", len(raw), "TEST ONLY"))
        doc = uow.knowledge.register_document({"source_id": SOURCE_ID, "asset_id": asset["id"],
            "title": "synthetic", "publisher": "synthetic", "source_url": "https://example.invalid/original",
            "published_at": datetime.now(timezone.utc), "permission_note": "TEST ONLY", "document_version": "opaque-registered-version"})
    with pytest.raises(InvalidPermission):
        inputs.get_document_input_ref(store, doc["id"], authorized_document_ids=[])
    ref = inputs.get_document_input_ref(store, doc["id"], authorized_document_ids=[doc["id"]])
    assert ref["sha256"] == hashlib.sha256(raw).hexdigest()
    assert ref["document_version"] == "opaque-registered-version"
    Path(ref["path"]).write_bytes(b"changed")
    with pytest.raises(Conflict):
        inputs.get_document_input_ref(store, doc["id"], authorized_document_ids=[doc["id"]])
