from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from data_foundation import CountInput, InvalidArgument, JobCreate, JobLease, LeaseLost
from data_foundation.db.models import Job, JobAttempt, KnowledgeChunk
from conftest import CAMERA_ID, SCENIC_ID, SOURCE_ID, register_asset


ZERO = {"requeued": 0, "failed": 0}


def running(store, *, kind="knowledge_index", now=None, seconds=60):
    now = now or datetime.now(timezone.utc)
    with store.write() as uow:
        job = uow.jobs.enqueue_or_get(JobCreate(kind, uuid4().hex, {}, "仅恢复测试"), now)
        result = uow.jobs.claim_job("test-worker", now, seconds, job_id=job["id"])
    return now, JobLease(**result["lease"]), now+timedelta(seconds=seconds)


def test_boundary_requeue_preserves_history_and_is_idempotent(store):
    start, lease, expiry = running(store)
    with store.write() as uow:
        job = uow.session.get(Job, lease.job_id)
        job.processed_count, job.imported_count = 12, 7
        job.result_refs, job.error_summary = {"partial": "already_committed"}, "old_run"
        assert uow.jobs.recover_expired(expiry-timedelta(microseconds=1)) == ZERO
        assert uow.jobs.recover_expired(expiry) == {"requeued": 1, "failed": 0}
        assert uow.jobs.recover_expired(expiry) == ZERO
    with store.read() as uow:
        job = uow.jobs.get_job(lease.job_id)
        assert job["status"] == "queued" and job["attempt_count"] == 1
        assert job["available_at"] == expiry
        assert job["started_at"] is job["finished_at"] is job["result_refs"] is job["error_summary"] is None
        assert job["processed_count"] == job["imported_count"] == 0
        attempt = uow.session.scalar(select(JobAttempt).where(JobAttempt.job_id == lease.job_id))
        assert attempt.outcome == "expired" and attempt.error_summary == "lease_expired"
        assert attempt.started_at == start.replace(tzinfo=None)
        assert attempt.finished_at == expiry.replace(tzinfo=None) and attempt.lease_token == lease.lease_token
    with store.write() as uow:
        new = uow.jobs.claim_job("new-worker", expiry, job_id=lease.job_id)
        assert new["job"]["attempt_count"] == 2 and new["lease"]["lease_token"] != lease.lease_token


def test_attempt_limit_stops_auto_requeue_and_keeps_attempts(store):
    _, first, expiry = running(store)
    current = first
    for attempt_no in (1, 2, 3):
        with store.write() as uow:
            result = uow.jobs.recover_expired(expiry)
            assert result == ({"requeued": 1, "failed": 0} if attempt_no < 3 else {"requeued": 0, "failed": 1})
        if attempt_no < 3:
            with store.write() as uow:
                new = uow.jobs.claim_job("worker", expiry, 60, job_id=current.job_id)
                current = JobLease(**new["lease"])
                assert current.attempt_no == attempt_no+1
            expiry += timedelta(seconds=60)
    with store.write() as uow:
        assert uow.jobs.recover_expired(expiry) == ZERO
        assert uow.jobs.claim_job("worker", expiry, job_id=first.job_id) is None
        job = uow.jobs.get_job(first.job_id)
        assert job["status"] == "failed" and job["attempt_count"] == 3
        assert job["error_summary"] == "lease_expired" and job["finished_at"] == expiry
        assert uow.session.scalar(select(func.count()).select_from(JobAttempt)) == 3


@pytest.mark.parametrize("state", ["queued", "succeeded", "failed"])
def test_nonrunning_jobs_are_untouched(store, state):
    _, lease, expiry = running(store)
    with store.write() as uow:
        uow.session.get(Job, lease.job_id).status = state
        assert uow.jobs.recover_expired(expiry) == ZERO
    with store.read() as uow:
        assert uow.session.get(Job, lease.job_id).status == state
        assert uow.session.scalar(select(JobAttempt)).outcome is None


@pytest.mark.parametrize("field", ["finished_at", "outcome", "lease_expires_at", "attempt_count"])
def test_only_current_unfinished_expired_attempt_is_recovered(store, field):
    _, lease, expiry = running(store)
    with store.write() as uow:
        attempt = uow.session.scalar(select(JobAttempt))
        if field == "finished_at":
            attempt.finished_at = expiry.replace(tzinfo=None)
        elif field == "outcome":
            attempt.outcome = "succeeded"
        elif field == "lease_expires_at":
            attempt.lease_expires_at += timedelta(microseconds=1)
        else:
            uow.session.get(Job, lease.job_id).attempt_count = 2
        assert uow.jobs.recover_expired(expiry) == ZERO


def test_batch_limit_orders_candidates_by_job_id(store):
    start = datetime.now(timezone.utc)
    items = [running(store, now=start) for _ in range(3)]
    ids = sorted(item[1].job_id for item in items)
    with store.write() as uow:
        assert uow.jobs.recover_expired(items[0][2], batch_size=2) == {"requeued": 2, "failed": 0}
        assert [uow.session.get(Job, job_id).status for job_id in ids] == ["queued", "queued", "running"]
        assert uow.jobs.recover_expired(items[0][2], batch_size=2) == {"requeued": 1, "failed": 0}


@pytest.mark.parametrize("field", ["max_attempts", "batch_size"])
@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "3", None])
def test_invalid_limits_make_no_changes(store, field, value):
    _, lease, expiry = running(store)
    with store.write() as uow:
        with pytest.raises(InvalidArgument):
            uow.jobs.recover_expired(expiry, **{field: value})
        assert uow.session.get(Job, lease.job_id).status == "running"
        assert uow.session.scalar(select(JobAttempt)).outcome is None


@pytest.mark.parametrize("now", [None, True, "2026-10-13", datetime(2026, 10, 13)])
def test_recovery_requires_aware_datetime(store, now):
    with store.write() as uow:
        with pytest.raises(InvalidArgument):
            uow.jobs.recover_expired(now)


def test_recovery_requires_write_and_rolls_back_atomically(store):
    _, lease, expiry = running(store)
    with store.read() as uow:
        with pytest.raises(InvalidArgument):
            uow.jobs.recover_expired(expiry)
    with pytest.raises(RuntimeError):
        with store.write() as uow:
            assert uow.jobs.recover_expired(expiry) == {"requeued": 1, "failed": 0}
            raise RuntimeError("模拟调用方失败")
    with store.read() as uow:
        assert uow.session.get(Job, lease.job_id).status == "running"
        assert uow.session.scalar(select(JobAttempt)).outcome is None


@pytest.mark.parametrize("method", ["ensure", "heartbeat", "complete", "fail"])
def test_old_lease_rejected_after_recovery_and_reclaim(store, method):
    _, lease, expiry = running(store)
    with store.write() as uow:
        uow.jobs.recover_expired(expiry)
        new = JobLease(**uow.jobs.claim_job("new-worker", expiry, job_id=lease.job_id)["lease"])
    with store.write() as uow:
        with pytest.raises(LeaseLost):
            if method == "ensure":
                uow.jobs.ensure_lease(lease, expiry)
            elif method == "heartbeat":
                uow.jobs.heartbeat(lease, expiry)
            elif method == "complete":
                uow.jobs.complete_job(lease, {}, expiry)
            else:
                uow.jobs.fail_job(lease, "old", expiry)
        assert uow.jobs.ensure_lease(new, expiry).attempt_no == 2
        uow.jobs.complete_job(new, {"new": True}, expiry)


def test_lease_check_refreshes_cached_attempt_and_checks_current_attempt(store):
    _, lease, expiry = running(store)
    with store.write() as uow:
        cached = uow.jobs.ensure_lease(lease, expiry-timedelta(microseconds=1))
        # 低层 SQL 模拟会话外部更新，旧 identity-map 不可绕过校验。
        from sqlalchemy import update
        uow.session.execute(update(JobAttempt).where(JobAttempt.id == cached.id).values(
            outcome="expired", finished_at=expiry.replace(tzinfo=None)
        ).execution_options(synchronize_session=False))
        with pytest.raises(LeaseLost):
            uow.jobs.ensure_lease(lease, expiry-timedelta(microseconds=1))
        uow.session.execute(update(JobAttempt).where(JobAttempt.id == cached.id).values(
            outcome=None, finished_at=None
        ).execution_options(synchronize_session=False))
        uow.session.execute(update(Job).where(Job.id == lease.job_id).values(
            attempt_count=lease.attempt_no+1
        ).execution_options(synchronize_session=False))
        with pytest.raises(LeaseLost):
            uow.jobs.ensure_lease(lease, expiry-timedelta(microseconds=1))


def test_business_writes_reject_old_lease_and_keep_committed_chunks(store, monkeypatch):
    start, lease, expiry = running(store)
    asset = register_asset(store)
    with store.write() as uow:
        doc = uow.knowledge.register_document({"source_id": SOURCE_ID, "asset_id":asset,
            "title":"恢复测试", "publisher":"测试", "source_url":"https://example.invalid/recovery",
            "published_at":start, "permission_note":"仅测试", "document_version":"v1"}, [SCENIC_ID])
        index = uow.knowledge.begin_index(document_id=doc["id"], version="v1", job_id=lease.job_id,
            parser_version="test", embedding_model_version="test", lease=lease)
        uow.knowledge.write_chunks(index["id"], [{"chunk_no":0,"text":"已提交片段","locator":{"page":1}}],lease)
    with store.write() as uow:
        uow.jobs.recover_expired(expiry)
        new = JobLease(**uow.jobs.claim_job("new", expiry, job_id=lease.job_id)["lease"])
    monkeypatch.setattr("data_foundation.repositories.utcnow_naive", lambda: expiry.replace(tzinfo=None))
    with store.write() as uow:
        for operation in (
            lambda: uow.knowledge.begin_index(document_id=doc["id"],version="new",job_id=lease.job_id,parser_version="test",embedding_model_version="test",lease=lease),
            lambda: uow.knowledge.write_chunks(index["id"],[{"chunk_no":1,"text":"旧执行者","locator":{}}],lease),
            lambda: uow.knowledge.activate_index(document_id=doc["id"],version="v1",artifact_path="test",artifact_sha256="a"*64,expected_chunk_count=1,lease=lease),
        ):
            with pytest.raises(LeaseLost):operation()
        assert uow.session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 1
        uow.knowledge.write_chunks(index["id"],[{"chunk_no":1,"text":"新执行者","locator":{}}],new)
        uow.knowledge.activate_index(document_id=doc["id"],version="v1",artifact_path="test",artifact_sha256="a"*64,expected_chunk_count=2,lease=new)
        uow.jobs.complete_job(new,{"document_id":doc["id"]},expiry)
    with store.read() as uow:
        assert len(uow.knowledge.get_chunks(doc["id"])["items"]) == 2


def test_video_writes_reject_old_lease_and_new_lease_can_complete(store, monkeypatch):
    start, lease, expiry = running(store, kind="video_analysis")
    asset = register_asset(store)
    with store.write() as uow:
        permission = uow.video.register_permission({"source_id":SOURCE_ID,"basis":"测试","grantor":"测试",
            "evidence_path":"test","evidence_sha256":"a"*64,"allowed_uses":["analysis"],
            "valid_from":start-timedelta(days=1),"valid_until":start+timedelta(days=1),"status":"approved","playback_allowed":False})
        video = uow.video.register_video({"source_id":SOURCE_ID,"asset_id":asset,"scenic_id":SCENIC_ID,
            "camera_id":CAMERA_ID,"permission_record_id":permission["id"],"recorded_start_at":start,
            "recorded_end_at":start+timedelta(minutes=10),"camera_location_note":"测试"},start)
        analysis = uow.video.create_analysis(video["id"],lease.job_id,{"pipeline_version":"test","detector_model_version":"test",
            "detector_sha256":"a"*64,"openvino_version":"test","device":"CPU","tracker_config_id":None,
            "tracker_config":{},"count_line":{"x1":0,"y1":0,"x2":1,"y2":1},"entry_side":"left","interval_minutes":5})
    with store.write() as uow:
        uow.jobs.recover_expired(expiry)
        new = JobLease(**uow.jobs.claim_job("new",expiry,job_id=lease.job_id)["lease"])
    monkeypatch.setattr("data_foundation.repositories.utcnow_naive", lambda: expiry.replace(tzinfo=None))
    point = CountInput(start,start+timedelta(minutes=5),4,2)
    with store.write() as uow:
        with pytest.raises(LeaseLost):uow.video.write_counts(analysis["id"],[point],lease)
        with pytest.raises(LeaseLost):uow.video.complete_analysis(analysis["id"],0,lease)
        uow.video.write_counts(analysis["id"],[point],new)
        uow.video.complete_analysis(analysis["id"],1,new)
        uow.jobs.complete_job(new,{"analysis_id":analysis["id"]},expiry)
    with store.read() as uow:
        assert uow.video.query_counts(video["id"],analysis["id"])["points"][0]["entries"] == 4
