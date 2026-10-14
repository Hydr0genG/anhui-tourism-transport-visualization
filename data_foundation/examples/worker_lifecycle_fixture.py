"""合成存储验收材料；不是授权录像或算法效果数据。"""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import uuid4

from data_foundation import AssetCreate, CountInput, EntityCreate, JobCreate, JobLease, SourceCreate, UserCreate


def make_fixture(store):
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=5)
    source_id, scenic_id, camera_id = (str(uuid4()) for _ in range(3))
    media = b"synthetic-video-content-not-playable"
    persisted = store.persist_file(source_id=source_id, original_name="test.mp4", stream=BytesIO(media))
    artifact = store.persist_file(source_id=source_id, original_name="index-test.bin",
        stream=BytesIO(b"synthetic-index-not-a-real-vector-model"))
    with store.write() as uow:
        uow.provenance.register_source(SourceCreate(source_id, "合成验收", "仅合成测试资料", "https://example.invalid/lifecycle", "不是真实数据", "irregular"))
        uow.catalog.create_entity(EntityCreate(scenic_id, "合成景区", "scenic", "340000"))
        uow.catalog.create_entity(EntityCreate(camera_id, "合成摄像头", "camera", "340000", parent_entity_id=scenic_id))
        asset = uow.provenance.register_asset(AssetCreate(source_id, "https://example.invalid/lifecycle/"+uuid4().hex,
            "合成原件", now, persisted["relative_path"], persisted["sha256"], "video/mp4", persisted["byte_size"], "不是真实录像"))
        admin = uow.auth.create_user(UserCreate("admin-"+uuid4().hex[:16], "合成管理员", "test-hash", "admin"))
        viewer = uow.auth.create_user(UserCreate("viewer-"+uuid4().hex[:16], "合成读者", "test-hash"))
        def running(kind):
            job = uow.jobs.enqueue_or_get(JobCreate(kind, uuid4().hex, {"synthetic": True}, "仅验收"), start)
            return JobLease(**uow.jobs.claim_job("synthetic-worker", start, 3600, job_id=job["id"])["lease"])
        knowledge_lease, video_lease, foreign_lease = (running(kind) for kind in ("knowledge_index", "video_analysis", "knowledge_index"))
        document = uow.knowledge.register_document({"source_id": source_id, "asset_id": asset["id"],
            "title": "合成知识文档", "publisher": "合成验收", "source_url": asset["url"], "published_at": now,
            "permission_note": "仅合成验收", "document_version": "test"})
        index = uow.knowledge.begin_index(document_id=document["id"], version="test", job_id=knowledge_lease.job_id,
            parser_version="parser-test", embedding_model_version="model-test", lease=knowledge_lease)
        chunk = {"chunk_no": 0, "text": "仅合成测试片段", "locator": {"page": 1}}
        uow.knowledge.write_chunks(index["id"], [chunk], knowledge_lease)
        permission = uow.video.register_permission({"source_id": source_id, "basis": "合成授权", "grantor": "合成授权方",
            "evidence_path": "test-only", "evidence_sha256": "a"*64, "allowed_uses": ["analysis", "replay"],
            "valid_from": now-timedelta(days=1), "valid_until": now+timedelta(days=1), "playback_allowed": True, "status": "approved"})
        video = uow.video.register_video({"source_id": source_id, "asset_id": asset["id"], "scenic_id": scenic_id,
            "camera_id": camera_id, "permission_record_id": permission["id"], "recorded_start_at": now,
            "recorded_end_at": now+timedelta(minutes=10)}, now)
        config = {"pipeline_version": "test", "detector_model_version": "test", "detector_sha256": "a"*64,
            "openvino_version": "test", "device": "CPU", "tracker_config": {}, "count_line": {"test": True},
            "entry_side": "left", "interval_minutes": 5}
        analysis = uow.video.create_analysis(video["id"], video_lease.job_id, config)
    return {"now": now, "start": start, "knowledge_lease": knowledge_lease, "video_lease": video_lease,
        "foreign_lease": foreign_lease, "admin": admin, "viewer": viewer, "document": document, "index": index,
        "chunk": chunk, "permission": permission, "video": video, "analysis": analysis, "asset": asset,
        "scenic_id": scenic_id, "camera_id": camera_id, "config": config, "artifact": artifact,
        "point": CountInput(now, now+timedelta(minutes=5), 10, 3)}
