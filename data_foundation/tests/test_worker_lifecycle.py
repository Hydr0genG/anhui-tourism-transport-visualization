from datetime import datetime, timedelta
from pathlib import Path
import os
import sys
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from data_foundation import Conflict, InvalidArgument, InvalidPermission, JobLease, LeaseLost, NotFound, Page, create_store
from data_foundation.db.models import Job, JobAttempt, JobRetry, KnowledgeChunk, KnowledgeIndex, PermissionRecord, User, VideoAnalysis
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"examples"))
from worker_lifecycle_fixture import make_fixture


@pytest.fixture
def fx(store, tmp_path):
    # MYSQL_LIFECYCLE_URL 仅由验收脚本创建并迁移独立空测试库后设置。
    url = os.environ.get("MYSQL_LIFECYCLE_URL")
    if url:
        from sqlalchemy.engine import make_url
        parsed = make_url(url)
        assert parsed.drivername == "mysql+pymysql" and parsed.database.endswith("_test")
        value = create_store(url, data_root=tmp_path)
    else:
        value = store
        value.data_root = tmp_path
    data = make_fixture(value)
    yield value, data
    if url:
        with value.write() as uow:
            for key in ("knowledge_lease", "video_lease", "foreign_lease"):
                uow.session.get(Job, data[key].job_id).status = "succeeded"
        value.dispose()


def fail_and_retry(store, f, kind="knowledge"):
    lease = f[kind+"_lease"]
    with store.write() as uow:
        uow.jobs.fail_job(lease, "test failure", f["start"])
        queued = uow.jobs.retry_failed(lease.job_id, "人工检查后重试", f["admin"]["id"], f["start"])
        assert queued["status"] == "queued"
        return JobLease(**uow.jobs.claim_job("retry-worker", f["start"], 3600, job_id=lease.job_id)["lease"])


def test_progress_absolute_monotone_and_no_lease_extension(fx):
    store, f = fx
    with store.write() as uow:
        before = uow.jobs.ensure_lease(f["knowledge_lease"], f["start"]).lease_expires_at
        assert uow.jobs.update_progress(f["knowledge_lease"], 5, 2, f["start"])["processed_count"] == 5
        assert uow.jobs.update_progress(f["knowledge_lease"], 5, 2, f["start"])["imported_count"] == 2
        assert uow.jobs.update_progress(f["knowledge_lease"], 8, 3, f["start"])["processed_count"] == 8
        assert uow.jobs.ensure_lease(f["knowledge_lease"], f["start"]).lease_expires_at == before
        for processed, imported in [(7, 3), (8, 2)]:
            with pytest.raises(Conflict):
                uow.jobs.update_progress(f["knowledge_lease"], processed, imported, f["start"])


@pytest.mark.parametrize("processed,imported", [(-1,0),(0,-1),(True,0),(1,False),(1.0,0),(2,"1"),(None,0),(0,1)])
def test_invalid_progress_unchanged(fx, processed, imported):
    store, f = fx
    with store.write() as uow:
        with pytest.raises(InvalidArgument):
            uow.jobs.update_progress(f["knowledge_lease"], processed, imported, f["start"])
        assert uow.jobs.get_job(f["knowledge_lease"].job_id)["processed_count"] == 0


def test_retry_preserves_history_payload_and_creates_new_lease_only_at_claim(fx):
    store, f = fx
    lease = f["knowledge_lease"]
    with store.write() as uow:
        uow.jobs.update_progress(lease, 10, 4, f["start"])
        uow.jobs.fail_job(lease, "failed-before-retry", f["start"])
        before = uow.session.get(Job, lease.job_id).payload.copy()
        job = uow.jobs.retry_failed(lease.job_id, "核验后重试", f["admin"]["id"], f["start"])
        assert job["attempt_count"] == 1 and job["status"] == "queued"
        assert job["started_at"] is job["finished_at"] is job["error_summary"] is job["result_refs"] is None
        assert job["processed_count"] == job["imported_count"] == 0
        assert uow.session.get(Job, lease.job_id).payload == before
        history = job["retry_history"]
        assert len(history) == 1 and history[0]["operator_id"] == f["admin"]["id"]
        assert history[0]["reason"] == "核验后重试" and history[0]["requested_at"] == f["start"]
        assert uow.session.scalar(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id == lease.job_id)) == 1
        current = JobLease(**uow.jobs.claim_job("retry", f["start"], job_id=lease.job_id)["lease"])
        assert current.attempt_no == 2 and current.lease_token != lease.lease_token
        with pytest.raises(LeaseLost):
            uow.jobs.update_progress(lease, 11, 5, f["start"])
        uow.jobs.update_progress(current, 1, 1, f["start"])


@pytest.mark.parametrize("reason", ["", "  ", None, True, "x"*2001])
def test_retry_bad_reason(fx, reason):
    store, f = fx
    with store.write() as uow:
        uow.jobs.fail_job(f["knowledge_lease"], "test", f["start"])
        with pytest.raises(InvalidArgument):
            uow.jobs.retry_failed(f["knowledge_lease"].job_id, reason, f["admin"]["id"], f["start"])
        assert uow.jobs.get_job(f["knowledge_lease"].job_id)["retry_history"] == []


@pytest.mark.parametrize("state", ["queued", "running", "succeeded"])
def test_retry_rejects_nonfailed_states(fx, state):
    store, f = fx
    with store.write() as uow:
        uow.session.get(Job, f["knowledge_lease"].job_id).status = state
        with pytest.raises(Conflict):
            uow.jobs.retry_failed(f["knowledge_lease"].job_id, "retry", f["admin"]["id"], f["start"])


@pytest.mark.parametrize("operator", ["viewer", "disabled", "missing", "blank"])
def test_retry_requires_active_admin(fx, operator):
    store, f = fx
    with store.write() as uow:
        uow.jobs.fail_job(f["knowledge_lease"], "test", f["start"])
        operator_id = f["admin"]["id"]
        expected = InvalidPermission
        if operator == "viewer": operator_id = f["viewer"]["id"]
        if operator == "disabled": uow.session.get(User, operator_id).status = "disabled"
        if operator == "missing": operator_id, expected = str(uuid4()), NotFound
        if operator == "blank": operator_id, expected = " ", InvalidArgument
        with pytest.raises(expected):
            uow.jobs.retry_failed(f["knowledge_lease"].job_id, "retry", operator_id, f["start"])


def test_retry_progress_and_failure_roll_back_together(fx):
    store, f = fx
    with pytest.raises(RuntimeError):
        with store.write() as uow:
            uow.jobs.update_progress(f["knowledge_lease"], 3, 1, f["start"])
            uow.knowledge.fail_index(f["index"]["id"], "error", f["knowledge_lease"])
            uow.jobs.fail_job(f["knowledge_lease"], "error", f["start"])
            uow.jobs.retry_failed(f["knowledge_lease"].job_id, "retry", f["admin"]["id"], f["start"])
            raise RuntimeError("caller transaction fails")
    with store.read() as uow:
        job = uow.jobs.get_job(f["knowledge_lease"].job_id)
        assert job["status"] == "running" and job["processed_count"] == 0 and job["retry_history"] == []
        assert uow.session.get(KnowledgeIndex, f["index"]["id"]).status == "building"


@pytest.mark.parametrize("method", ["begin", "write", "activate", "fail", "video_begin", "video_write", "video_complete", "video_fail"])
def test_foreign_current_lease_cannot_write_business(fx, method):
    store, f = fx
    foreign = f["foreign_lease"]
    with store.write() as uow:
        with pytest.raises(LeaseLost):
            if method == "begin": uow.knowledge.begin_index(document_id=f["document"]["id"], version="other", job_id=f["knowledge_lease"].job_id, parser_version="p", embedding_model_version="m", lease=foreign)
            if method == "write": uow.knowledge.write_chunks(f["index"]["id"], [], foreign)
            if method == "activate": uow.knowledge.activate_index(document_id=f["document"]["id"], version="test", artifact_path=f["artifact"]["relative_path"], artifact_sha256=f["artifact"]["sha256"], expected_chunk_count=1, lease=foreign)
            if method == "fail": uow.knowledge.fail_index(f["index"]["id"], "x", foreign)
            if method == "video_begin": uow.video.begin_analysis(f["analysis"]["id"], foreign)
            if method == "video_write": uow.video.write_counts(f["analysis"]["id"], [f["point"]], foreign)
            if method == "video_complete": uow.video.complete_analysis(f["analysis"]["id"], 0, foreign)
            if method == "video_fail": uow.video.fail_analysis(f["analysis"]["id"], "x", foreign)
        assert uow.session.get(KnowledgeIndex, f["index"]["id"]).status == "building"
        assert uow.session.get(VideoAnalysis, f["analysis"]["id"]).status == "queued"


def test_knowledge_failed_retry_retains_chunks_and_publishes_atomically(fx):
    store, f = fx
    with store.write() as uow:
        uow.knowledge.fail_index(f["index"]["id"], "parse failure", f["knowledge_lease"])
        uow.jobs.fail_job(f["knowledge_lease"], "parse failure", f["start"])
        uow.jobs.retry_failed(f["knowledge_lease"].job_id, "retry", f["admin"]["id"], f["start"])
        lease = JobLease(**uow.jobs.claim_job("retry", f["start"], job_id=f["knowledge_lease"].job_id)["lease"])
        same = uow.knowledge.begin_index(document_id=f["document"]["id"], version="test", job_id=lease.job_id, parser_version="parser-test", embedding_model_version="model-test", lease=lease)
        assert same["id"] == f["index"]["id"] and same["error_summary"] is None
        assert uow.knowledge.write_chunks(same["id"], [f["chunk"]], lease) == {"inserted":0,"skipped":1}
        with pytest.raises(Conflict):
            uow.knowledge.write_chunks(same["id"], [{**f["chunk"], "text":"different"}], lease)
        uow.knowledge.activate_index(document_id=f["document"]["id"], version="test", artifact_path=f["artifact"]["relative_path"], artifact_sha256=f["artifact"]["sha256"], expected_chunk_count=1, lease=lease)
        uow.jobs.complete_job(lease, {"document_id": f["document"]["id"]}, f["start"])
    with store.read() as uow:
        assert uow.knowledge.get_chunks(f["document"]["id"])["status"] == "ready"
        assert uow.jobs.get_job(lease.job_id)["status"] == "succeeded"


def test_index_failure_preserves_previous_active_version(fx):
    store, f = fx
    with store.write() as uow:
        uow.knowledge.activate_index(document_id=f["document"]["id"], version="test", artifact_path=f["artifact"]["relative_path"], artifact_sha256=f["artifact"]["sha256"], expected_chunk_count=1, lease=f["knowledge_lease"])
        index = uow.knowledge.begin_index(document_id=f["document"]["id"], version="next", job_id=f["foreign_lease"].job_id, parser_version="p", embedding_model_version="m", lease=f["foreign_lease"])
        uow.knowledge.fail_index(index["id"], "new version failed", f["foreign_lease"])
        assert uow.knowledge.get_document(f["document"]["id"])["active_index_version"] == "test"
        assert uow.knowledge.get_chunks(f["document"]["id"])["status"] == "ready"
        with pytest.raises(Conflict):
            uow.knowledge.fail_index(f["index"]["id"], "cannot unpublish", f["knowledge_lease"])


def test_video_failed_retry_retains_counts_and_requires_begin(fx):
    store, f = fx
    with store.write() as uow:
        uow.video.begin_analysis(f["analysis"]["id"], f["video_lease"], f["start"])
        uow.video.write_counts(f["analysis"]["id"], [f["point"]], f["video_lease"])
        failed = uow.video.fail_analysis(f["analysis"]["id"], "model failed", f["video_lease"])
        assert failed["error_summary"] == "model failed"
        uow.jobs.fail_job(f["video_lease"], "model failed", f["start"])
    with store.write() as uow:
        uow.jobs.retry_failed(f["video_lease"].job_id, "retry", f["admin"]["id"], f["start"])
        lease = JobLease(**uow.jobs.claim_job("retry", f["start"], job_id=f["video_lease"].job_id)["lease"])
        with pytest.raises(Conflict): uow.video.write_counts(f["analysis"]["id"], [f["point"]], lease)
        uow.video.begin_analysis(f["analysis"]["id"], lease, f["start"])
        assert uow.video.write_counts(f["analysis"]["id"], [f["point"]], lease) == {"inserted":0,"skipped":1}
        uow.video.complete_analysis(f["analysis"]["id"], 1, lease)
        uow.jobs.complete_job(lease, {"analysis_id":f["analysis"]["id"]}, f["start"])
    with store.read() as uow:
        assert len(uow.video.query_counts(f["video"]["id"], f["analysis"]["id"])["points"]) == 1


@pytest.mark.parametrize("change", ["expired", "revoked", "not_started", "use", "playback_flag"])
def test_playback_requires_current_permission(fx, change):
    store, f = fx
    with store.write() as uow:
        p = uow.session.get(PermissionRecord, f["permission"]["id"])
        if change == "expired": p.valid_until = f["now"].replace(tzinfo=None)
        if change == "revoked": p.status = "revoked"
        if change == "not_started": p.valid_from = (f["now"]+timedelta(microseconds=1)).replace(tzinfo=None)
        if change == "use": p.allowed_uses = ["analysis"]
        if change == "playback_flag": p.playback_allowed = False
    with store.read() as uow:
        with pytest.raises(InvalidPermission): uow.video.get_playback_ref(f["video"]["id"], f["now"])


def test_playback_private_ref_hash_path_and_safe_metadata(fx, tmp_path):
    store, f = fx
    with store.read() as uow:
        ref = uow.video.get_playback_ref(f["video"]["id"], f["now"])
        assert Path(ref["path"]).is_relative_to(store.data_root.resolve())
        assert "path" not in uow.video.get_video(f["video"]["id"])
        assert "evidence_path" not in uow.video.get_permission(f["permission"]["id"])
        assert uow.video.list_videos(scenic_id=f["scenic_id"], camera_id=f["camera_id"], page=Page(1,1))["total"] == 1
        Path(ref["path"]).write_bytes(b"tampered")
        with pytest.raises(Conflict): uow.video.get_playback_ref(f["video"]["id"], f["now"])


@pytest.mark.parametrize("method", ["begin", "write", "complete"])
def test_analysis_rechecks_revoked_permission(fx, method):
    store, f = fx
    with store.write() as uow:
        uow.session.get(PermissionRecord, f["permission"]["id"]).status = "revoked"
    with store.write() as uow:
        with pytest.raises(InvalidPermission):
            if method == "begin": uow.video.begin_analysis(f["analysis"]["id"], f["video_lease"])
            if method == "write": uow.video.write_counts(f["analysis"]["id"], [f["point"]], f["video_lease"])
            if method == "complete": uow.video.complete_analysis(f["analysis"]["id"], 0, f["video_lease"])
        # 失去授权仍可记录失败，停止处理。
        assert uow.video.fail_analysis(f["analysis"]["id"], "permission revoked", f["video_lease"])["status"] == "failed"


def test_read_only_rejects_new_write_paths(fx):
    store, f = fx
    with store.read() as uow:
        for call in [lambda:uow.jobs.update_progress(f["knowledge_lease"],0,0),
            lambda:uow.jobs.retry_failed(f["knowledge_lease"].job_id,"test",f["admin"]["id"],f["start"]),
            lambda:uow.knowledge.fail_index(f["index"]["id"],"test",f["knowledge_lease"]),
            lambda:uow.video.begin_analysis(f["analysis"]["id"],f["video_lease"]),
            lambda:uow.video.fail_analysis(f["analysis"]["id"],"test",f["video_lease"])]:
            with pytest.raises(InvalidArgument): call()


@pytest.mark.parametrize("seconds", [0,-1,True,1.2,"60"])
def test_invalid_lease_duration_rejected(fx, seconds):
    store, f = fx
    with store.write() as uow:
        with pytest.raises(InvalidArgument): uow.jobs.heartbeat(f["knowledge_lease"], f["start"], seconds)
        with pytest.raises(InvalidArgument): uow.jobs.claim_job("worker", f["start"], seconds)


def test_progress_expiry_boundary_and_retry_keeps_global_attempt_cap(fx):
    store, f = fx
    lease = f["knowledge_lease"]
    expiry = f["start"]+timedelta(seconds=3600)
    with store.write() as uow:
        uow.jobs.update_progress(lease,1,1,expiry-timedelta(microseconds=1))
        with pytest.raises(LeaseLost): uow.jobs.update_progress(lease,2,1,expiry)
        assert uow.jobs.recover_expired(expiry,max_attempts=1)["failed"] == 3
        uow.jobs.retry_failed(lease.job_id,"one more explicit attempt",f["admin"]["id"],expiry)
        current = JobLease(**uow.jobs.claim_job("retry",expiry,1,job_id=lease.job_id)["lease"])
        assert current.attempt_no == 2
        assert uow.jobs.recover_expired(expiry+timedelta(seconds=1),max_attempts=1) == {"requeued":0,"failed":1}


@pytest.mark.parametrize("now", [None,True,"2026-10-14",datetime(2026,10,14)])
def test_retry_requires_aware_time(fx, now):
    store,f = fx
    with store.write() as uow:
        with pytest.raises(InvalidArgument): uow.jobs.retry_failed(f["knowledge_lease"].job_id,"retry",f["admin"]["id"],now)


@pytest.mark.parametrize("path_kind", ["absolute", "traversal", "symlink", "missing", "no_root"])
def test_playback_rejects_invalid_file_references(fx, tmp_path, path_kind):
    from data_foundation.db.models import SourceAsset
    store,f = fx
    if path_kind == "no_root": store.data_root = None
    with store.write() as uow:
        asset = uow.session.get(SourceAsset,f["asset"]["id"])
        if path_kind == "absolute": asset.relative_path = str(tmp_path/"file.mp4")
        if path_kind == "traversal": asset.relative_path = "../outside.mp4"
        if path_kind == "missing": asset.relative_path = "not-present.mp4"
        if path_kind == "symlink":
            target = tmp_path.parent/(uuid4().hex+".mp4")
            target.write_bytes(b"outside")
            link = tmp_path/"unsafe.mp4"
            link.symlink_to(target)
            asset.relative_path = "unsafe.mp4"
    with store.read() as uow:
        with pytest.raises(NotFound if path_kind == "missing" else InvalidArgument):
            uow.video.get_playback_ref(f["video"]["id"],f["now"])


def test_create_analysis_conflicting_owner_and_failed_results_hidden(fx):
    from data_foundation import AnalysisNotReady
    store,f = fx
    with store.write() as uow:
        with pytest.raises(Conflict): uow.video.create_analysis(f["video"]["id"],f["foreign_lease"].job_id,f["config"])
        uow.video.write_counts(f["analysis"]["id"],[f["point"]],f["video_lease"])
        uow.video.fail_analysis(f["analysis"]["id"],"failure",f["video_lease"])
    with store.read() as uow:
        with pytest.raises(AnalysisNotReady): uow.video.query_counts(f["video"]["id"],f["analysis"]["id"])


def test_index_retry_cannot_change_models_or_take_another_jobs_version(fx):
    store,f = fx
    with store.write() as uow:
        with pytest.raises(Conflict): uow.knowledge.begin_index(document_id=f["document"]["id"],version="test",job_id=f["knowledge_lease"].job_id,parser_version="changed",embedding_model_version="model-test",lease=f["knowledge_lease"])
        with pytest.raises(LeaseLost): uow.knowledge.begin_index(document_id=f["document"]["id"],version="test",job_id=f["foreign_lease"].job_id,parser_version="parser-test",embedding_model_version="model-test",lease=f["foreign_lease"])


def test_failed_video_does_not_hide_other_completed_analysis(fx):
    store,f = fx
    with store.write() as uow:
        uow.video.complete_analysis(f["analysis"]["id"],0,f["video_lease"])
        another = uow.video.create_analysis(f["video"]["id"],f["foreign_lease"].job_id,{**f["config"],"pipeline_version":"next"})
        uow.video.fail_analysis(another["id"],"new model failed",f["foreign_lease"])
        assert uow.video.get_video(f["video"]["id"])["status"] == "ready"


def test_repeat_retry_is_conflict_and_no_second_audit(fx):
    store,f = fx
    with store.write() as uow:
        uow.jobs.fail_job(f["knowledge_lease"],"failed",f["start"])
        uow.jobs.retry_failed(f["knowledge_lease"].job_id,"retry",f["admin"]["id"],f["start"])
        with pytest.raises(Conflict): uow.jobs.retry_failed(f["knowledge_lease"].job_id,"retry",f["admin"]["id"],f["start"])
        assert len(uow.jobs.get_job(f["knowledge_lease"].job_id)["retry_history"]) == 1
