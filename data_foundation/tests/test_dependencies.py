from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from data_foundation import CountInput, JobCreate, JobLease

from conftest import CAMERA_ID, SCENIC_ID, SOURCE_ID, register_asset


def _claim(uow, job_type: str, key: str, now: datetime):
    job = uow.jobs.enqueue_or_get(JobCreate(job_type, key, {"test": True}, "自动化测试"), now)
    claimed = uow.jobs.claim_job("pytest-worker", now)
    assert claimed["job"]["id"] == job["id"]
    return JobLease(**claimed["lease"])


def test_knowledge_chunks_can_be_published_atomically(store):
    now = datetime.now(timezone.utc)
    asset_id = register_asset(store, sha="d" * 64, suffix="knowledge")
    with store.write() as uow:
        lease = _claim(uow, "knowledge_index", "d" * 64, now)
        document = uow.knowledge.register_document({
            "source_id": SOURCE_ID, "asset_id": asset_id, "title": "测试资料",
            "publisher": "测试发布单位", "source_url": "https://example.invalid/knowledge",
            "published_at": now, "valid_from": None, "valid_until": None,
            "permission_note": "仅测试", "document_version": "v1",
        }, [SCENIC_ID])
        index = uow.knowledge.begin_index(
            document_id=document["id"], version="idx-v1", job_id=lease.job_id,
            parser_version="parser-v1", embedding_model_version="embedding-test-v1", lease=lease,
        )
        result = uow.knowledge.write_chunks(index["id"], [
            {"chunk_no": 0, "text": "测试片段", "locator": {"page": 1}},
        ], lease)
        assert result == {"inserted": 1, "skipped": 0}
        uow.knowledge.activate_index(
            document_id=document["id"], version="idx-v1", artifact_path="indexes/test",
            artifact_sha256="e" * 64, expected_chunk_count=1, lease=lease,
        )
        uow.jobs.complete_job(lease, {"document_id": document["id"]}, now + timedelta(seconds=1))
    with store.read() as uow:
        chunks = uow.knowledge.get_chunks(document["id"])
        assert chunks["status"] == "ready"
        assert chunks["items"][0]["locator"] == {"page": 1}


def test_video_count_roundtrip_and_duplicate_skip(store):
    now = datetime.now(timezone.utc)
    asset_id = register_asset(store, sha="f" * 64, suffix="video")
    with store.write() as uow:
        permission = uow.video.register_permission({
            "source_id": SOURCE_ID, "basis": "测试授权", "grantor": "测试授权方",
            "evidence_path": "permissions/test.txt", "evidence_sha256": "1" * 64,
            "allowed_uses": ["analysis", "replay"], "valid_from": now - timedelta(days=1),
            "valid_until": now + timedelta(days=1), "playback_allowed": True, "status": "approved",
        })
        video = uow.video.register_video({
            "source_id": SOURCE_ID, "asset_id": asset_id, "scenic_id": SCENIC_ID,
            "camera_id": CAMERA_ID, "permission_record_id": permission["id"],
            "recorded_start_at": now, "recorded_end_at": now + timedelta(minutes=10),
            "camera_location_note": "测试入口",
        }, now)
        lease = _claim(uow, "video_analysis", "2" * 64, now)
        analysis = uow.video.create_analysis(video["id"], lease.job_id, {
            "pipeline_version": "test-v1", "detector_model_version": "yolov8-test",
            "detector_sha256": "3" * 64, "openvino_version": "test",
            "device": "CPU", "tracker_config_id": None, "tracker_config": {"name": "test"},
            "count_line": {"x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5},
            "entry_side": "left", "interval_minutes": 5,
        })
        point = CountInput(now, now + timedelta(minutes=5), 8, 3)
        assert uow.video.write_counts(analysis["id"], [point], lease)["inserted"] == 1
        assert uow.video.write_counts(analysis["id"], [point], lease)["skipped"] == 1
        uow.video.complete_analysis(analysis["id"], 1, lease, {"sample_count": 1, "mae": 1.0})
        uow.jobs.complete_job(lease, {"analysis_id": analysis["id"]}, now + timedelta(seconds=1))
    with store.read() as uow:
        result = uow.video.query_counts(video["id"], analysis["id"])
        assert result["points"][0]["entries"] == 8
        assert result["points"][0]["exits"] == 3


def test_holiday_event_and_file_provenance(store, tmp_path: Path):
    now = datetime.now(timezone.utc)
    store.data_root = tmp_path / "private-assets"
    staged = store.persist_file(
        source_id=SOURCE_ID, original_name="notice.pdf", stream=BytesIO(b"official-test-content")
    )
    assert (store.data_root / staged["relative_path"]).read_bytes() == b"official-test-content"
    asset_id = register_asset(store, sha=staged["sha256"], suffix="event")
    with store.write() as uow:
        holiday = uow.holidays.create_holiday({
            "name": "测试假期", "kind": "may_day", "year": 2026,
            "start_date": now.date(), "end_date": now.date(), "days_count": 1,
            "region_code": "340000", "scope_key": "test-scope",
            "definition_source_id": SOURCE_ID, "definition_asset_id": asset_id,
        })
        event = uow.events.create_event({
            "title": "测试公告", "description": "仅用于自动化测试", "kind": "other",
            "authority": "official", "severity": "notice", "region_code": "340000",
            "status": "active", "starts_at": now - timedelta(minutes=1), "ends_at": now + timedelta(minutes=1),
            "published_at": now, "last_verified_at": now, "source_id": SOURCE_ID,
            "asset_id": asset_id, "operator_note": None, "affected_entity_ids": [SCENIC_ID],
        }, operator_id=None, now=now)
        assert holiday["days_count"] == 1
        assert event["affected_entity_ids"] == [SCENIC_ID]
    with store.read() as uow:
        assert len(uow.holidays.list_holidays(year=2026)) == 1
        assert len(uow.events.list_events(status="active", now=now)) == 1
