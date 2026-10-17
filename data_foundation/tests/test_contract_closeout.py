"""C contract closeout; all write fixtures are synthetic, never official notices."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from data_foundation import Conflict, EntityCreate, InvalidArgument, SourceCreate
from data_foundation.db.models import Event, EventHistory
from conftest import SCENIC_ID, SOURCE_ID, register_asset


NOW = datetime(2026, 10, 17, 4, tzinfo=timezone.utc)


def event_values(store):
    return dict(title="合成测试公告", description="仅在隔离测试库中使用", kind="closure",
                authority="official", severity="notice", region_code="341000", status="active",
                starts_at=NOW, ends_at=NOW + timedelta(hours=1), source_id=SOURCE_ID,
                asset_id=register_asset(store), affected_entity_ids=[SCENIC_ID])


def create(store, values):
    with store.write() as uow:
        return uow.events.create_event(values, operator_id=None, now=NOW)


@pytest.mark.parametrize("region,expected", [
    ("340000", {"a", "b"}), ("34", {"a", "b"}),
    ("341000", {"a"}), ("3410", {"a"}), ("341002", {"a"}),
    ("340100", {"b"}), ("341003", set()), ("000000", {"a", "b", "c"}),
])
def test_administrative_hierarchy_keeps_province_city_and_county_boundaries(store, region, expected):
    identities = {label: str(uuid4()) for label in "abc"}
    with store.write() as uow:
        for label, code in [("a", "341002"), ("b", "340100"), ("c", "330100")]:
            uow.catalog.create_entity(EntityCreate(identities[label], "合成地区测试-" + label, "airport", code))
    with store.read() as uow:
        found = {row["id"] for row in uow.catalog.list_entities(type="airport", region_code=region, q="合成地区测试-")["items"]}
    assert found == {identities[label] for label in expected}


@pytest.mark.parametrize("region", ["", "3", "341", "34100", "3410000", "34%", "34_0", "３４", 34])
def test_invalid_region_is_a_domain_error_and_cannot_be_a_like_pattern(store, region):
    with store.read() as uow, pytest.raises(InvalidArgument):
        uow.catalog.list_entities(region_code=region)


@pytest.mark.parametrize("field", ["asset_id", "source_id", "starts_at", "authority"])
def test_missing_event_fields_do_not_escape_as_keyerror_or_integrityerror(store, field):
    values = event_values(store)
    values.pop(field)
    with pytest.raises(InvalidArgument) as caught:
        create(store, values)
    assert caught.value.details["field"] == field
    with store.read() as uow:
        assert uow.session.query(Event).count() == uow.session.query(EventHistory).count() == 0


@pytest.mark.parametrize("patch", [
    {"ends_at": NOW - timedelta(seconds=1)}, {"starts_at": NOW.replace(tzinfo=None)},
    {"kind": "invented"}, {"status": "invented"}, {"severity": "invented"},
    {"authority": "model_signal"}, {"title": " "}, {"description": None},
    {"affected_entity_ids": "not-an-array"}, {"ends_at": ""},
])
def test_invalid_creation_rolls_back_without_a_partial_event(store, patch):
    values = event_values(store)
    values.update(patch)
    with pytest.raises(InvalidArgument):
        create(store, values)
    with store.read() as uow:
        assert uow.session.query(Event).count() == 0


def test_default_active_and_expired_are_both_supported_without_rewriting_history(store):
    values = event_values(store)
    values.pop("status")
    first = create(store, values)
    values["status"] = "expired"
    second = create(store, values)
    with store.read() as uow:
        assert first["row_version"] == 1
        assert [item["id"] for item in uow.events.list_events(status="expired")] == [second["id"]]
        assert uow.events.list_events(status="active", now=NOW + timedelta(hours=2)) == []
        assert uow.events.get_event(first["id"])["status"] == "active"


def test_source_only_update_cannot_leave_asset_belonging_to_another_source(store):
    event = create(store, event_values(store))
    other = str(uuid4())
    with store.write() as uow:
        uow.provenance.register_source(SourceCreate(other, "合成", "合成来源", "https://example.invalid/other", "test", "irregular"))
    with store.write() as uow:
        with pytest.raises(InvalidArgument):
            uow.events.update_event(event["id"], {"source_id": other, "description": "不应写入", "change_note": "合成更新"},
                                    expected_version=1, operator_id=None, now=NOW)
        current = uow.events.get_event(event["id"])
        assert current["row_version"] == 1 and len(current["timeline"]) == 1
        assert current["description"] == event["description"] and current["source_id"] == SOURCE_ID
    with pytest.raises(InvalidArgument):
        with store.write() as uow:
            uow.events.update_event(event["id"], {"source_id": other, "asset_id": event["asset_id"], "change_note": "wrong binding"},
                                    expected_version=1, operator_id=None, now=NOW)


@pytest.mark.parametrize("version", [None, True, 0, -1, "1"])
def test_expected_version_requires_a_positive_integer(store, version):
    event = create(store, event_values(store))
    with store.write() as uow, pytest.raises(InvalidArgument):
        uow.events.update_event(event["id"], {"description": "合成更新", "change_note": "test"},
                                expected_version=version, operator_id=None, now=NOW)


def test_event_history_and_stale_version_and_reactivation_evidence(store):
    event = create(store, event_values(store))
    with store.write() as uow:
        resolved = uow.events.update_event(event["id"], {"status": "resolved", "change_note": "合成解除"},
                                          expected_version=1, operator_id=None, now=NOW)
    assert resolved["row_version"] == 2
    assert resolved["timeline"][-1]["snapshot"]["status"] == "resolved"
    with store.write() as uow, pytest.raises(Conflict) as caught:
        uow.events.update_event(event["id"], {"status": "active", "change_note": "stale"},
                                expected_version=1, operator_id=None, now=NOW)
    assert caught.value.details["current_version"] == 2
    with store.write() as uow, pytest.raises(InvalidArgument):
        uow.events.update_event(event["id"], {"status": "active", "change_note": "无新原件"},
                                expected_version=2, operator_id=None, now=NOW)
    new_asset = register_asset(store, sha="b" * 64, suffix="reactivation")
    with store.write() as uow:
        current = uow.events.update_event(event["id"], {
            "status": "active", "source_id": SOURCE_ID, "asset_id": new_asset,
            "affected_entity_ids": [SCENIC_ID, SCENIC_ID], "change_note": "合成新原件重新激活",
        }, expected_version=2, operator_id=None, now=NOW + timedelta(minutes=1))
    assert current["row_version"] == 3 and len(current["timeline"]) == 3
    assert current["affected_entity_ids"] == [SCENIC_ID]
    assert current["timeline"][0]["snapshot"]["asset_id"] == event["asset_id"]
    assert current["timeline"][-1]["snapshot"]["asset_id"] == new_asset


@pytest.mark.parametrize("patch", [
    {"ends_at": NOW - timedelta(seconds=1)}, {"description": None},
    {"status": "invented"}, {"change_note": None}, {"affected_entity_ids": "bad"},
])
def test_invalid_patch_is_rejected_before_mutating_a_live_event(store, patch):
    event = create(store, event_values(store))
    with store.write() as uow:
        with pytest.raises(InvalidArgument):
            uow.events.update_event(event["id"], {"description": "不应持久化", "change_note": "test", **patch},
                                    expected_version=1, operator_id=None, now=NOW)
        after = uow.events.get_event(event["id"])
        assert after["row_version"] == 1 and after["description"] == event["description"]
        assert len(after["timeline"]) == 1
