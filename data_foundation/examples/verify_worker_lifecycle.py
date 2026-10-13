"""C 0.1.6 独占空 MySQL 测试库竞争验收；不连接业务库，不清理已有库。"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from threading import Event

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from data_foundation import Conflict, CountInput, JobLease, LeaseLost, __version__, create_store
from data_foundation.db.models import CameraCount, Job, JobRetry, KnowledgeChunk, KnowledgeIndex
from de_handoff_smoke import migrate
from worker_lifecycle_fixture import make_fixture


def verify(url, data_root):
    parsed = make_url(url)
    if parsed.drivername != "mysql+pymysql" or not (parsed.database or "").endswith("_test"):
        raise ValueError("仅允许独占、名称以 _test 结尾的 MySQL 测试库")
    engine = create_engine(url)
    try:
        if inspect(engine).get_table_names(): raise ValueError("测试库必须为空，不删除已有数据")
    finally: engine.dispose()
    migrate(url)
    store = create_store(url,data_root=data_root)
    checks = []
    def passed(name): checks.append({"name":name,"status":"passed"})
    def configure(uow):
        uow.session.execute(text("SET SESSION innodb_lock_wait_timeout = 5"))
        return uow.session.scalar(text("SELECT CONNECTION_ID()"))
    try:
        f = make_fixture(store)
        lease = f["knowledge_lease"]
        with store.write() as uow: uow.jobs.fail_job(lease,"failure",f["start"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            started = Event()
            def second_retry():
                with store.write() as uow:
                    second_id = configure(uow)
                    started.set()
                    try: uow.jobs.retry_failed(lease.job_id,"second admin",f["admin"]["id"],f["start"])
                    except Conflict: return second_id,"conflict"
                    return second_id,"unexpected success"
            with store.write() as first:
                first_id = configure(first)
                first.jobs.retry_failed(lease.job_id,"first admin",f["admin"]["id"],f["start"])
                future = pool.submit(second_retry)
                assert started.wait(3)
            second_id,result = future.result(timeout=6)
            assert second_id != first_id and result == "conflict", (first_id,second_id,result)
        with store.read() as uow:
            assert len(uow.jobs.get_job(lease.job_id)["retry_history"]) == 1
        passed("concurrent_admin_retries_produce_one_requeue_and_audit")

        f = make_fixture(store)
        lease = f["knowledge_lease"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            cached,go = Event(),Event()
            def stale_progress():
                with store.write() as uow:
                    second_id = configure(uow)
                    assert uow.jobs.get_job(lease.job_id)["processed_count"] == 0
                    cached.set()
                    assert go.wait(3)
                    try: uow.jobs.update_progress(lease,10,2,f["start"])
                    except Conflict: return second_id,"conflict"
                    return second_id,"unexpected success"
            future = pool.submit(stale_progress)
            assert cached.wait(3)
            with store.write() as first:
                first_id = configure(first)
                first.jobs.update_progress(lease,20,4,f["start"])
                go.set()
            second_id,result = future.result(timeout=6)
            assert second_id != first_id and result == "conflict", (first_id,second_id,result)
        with store.read() as uow: assert uow.jobs.get_job(lease.job_id)["processed_count"] == 20
        passed("progress_rechecks_cached_state_after_waiting_for_job_lock")

        f = make_fixture(store)
        with ThreadPoolExecutor(max_workers=1) as pool:
            started = Event()
            def second_index():
                with store.write() as uow:
                    second_id = configure(uow)
                    started.set()
                    try:
                        uow.knowledge.begin_index(document_id=f["document"]["id"],version="competing",job_id=f["foreign_lease"].job_id,
                            parser_version="p",embedding_model_version="m",lease=f["foreign_lease"])
                    except LeaseLost: return second_id,"lease_lost"
                    return second_id,"unexpected success"
            with store.write() as first:
                first_id = configure(first)
                first.knowledge.begin_index(document_id=f["document"]["id"],version="competing",job_id=f["knowledge_lease"].job_id,
                    parser_version="p",embedding_model_version="m",lease=f["knowledge_lease"])
                future = pool.submit(second_index)
                assert started.wait(3)
            second_id,result = future.result(timeout=6)
            assert second_id != first_id and result == "lease_lost"
        passed("same_document_version_has_one_owner_across_jobs")

        # 旧缓存租约在管理员重试提交后，失败写入同样必须拒绝。
        f = make_fixture(store)
        old = f["knowledge_lease"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            cached,go = Event(),Event()
            def old_failure():
                with store.write() as uow:
                    configure(uow)
                    uow.session.get(KnowledgeIndex,f["index"]["id"])
                    uow.jobs.get_job(old.job_id)
                    cached.set()
                    assert go.wait(3)
                    try: uow.knowledge.fail_index(f["index"]["id"],"stale failure",old)
                    except LeaseLost: return "lease_lost"
                    return "unexpected success"
            future = pool.submit(old_failure)
            assert cached.wait(3)
            with store.write() as first:
                first.jobs.fail_job(old,"stopped",f["start"])
                first.jobs.retry_failed(old.job_id,"retry",f["admin"]["id"],f["start"])
                current = JobLease(**first.jobs.claim_job("retry",f["start"],3600,job_id=old.job_id)["lease"])
                go.set()
            assert future.result(timeout=6) == "lease_lost"
        with store.write() as uow:
            assert uow.jobs.ensure_lease(current,f["start"]).attempt_no == 2
            assert uow.session.get(KnowledgeIndex,f["index"]["id"]).status == "building"
        passed("stale_business_failure_rejected_after_manual_retry_and_reclaim")

        f = make_fixture(store)
        try:
            with store.write() as uow:
                uow.video.fail_analysis(f["analysis"]["id"],"test error",f["video_lease"])
                uow.jobs.fail_job(f["video_lease"],"test error",f["start"])
                uow.jobs.retry_failed(f["video_lease"].job_id,"retry",f["admin"]["id"],f["start"])
                raise RuntimeError("rollback")
        except RuntimeError: pass
        with store.read() as uow:
            assert uow.jobs.get_job(f["video_lease"].job_id)["retry_history"] == []
            assert uow.jobs.get_job(f["video_lease"].job_id)["status"] == "running"
            assert uow.video.get_video(f["video"]["id"])["analyses"][0]["status"] == "queued"
        passed("business_failure_job_failure_and_retry_audit_roll_back_atomically")
        # 普通查询先建立 REPEATABLE READ 旧快照；取得业务锁后必须使用当前读。
        for operation in ("chunk_duplicate", "chunk_count", "count_overlap"):
            f = make_fixture(store)
            with ThreadPoolExecutor(max_workers=1) as pool:
                cached,go = Event(),Event()
                def stale_business():
                    with store.write() as uow:
                        configure(uow)
                        if operation == "count_overlap":
                            assert uow.session.scalars(select(CameraCount).where(CameraCount.analysis_id==f["analysis"]["id"])).all() == []
                        else:
                            assert len(uow.session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.index_id==f["index"]["id"])).all()) == 1
                        cached.set()
                        assert go.wait(3)
                        if operation == "chunk_duplicate":
                            return uow.knowledge.write_chunks(f["index"]["id"],[{**f["chunk"],"chunk_no":1}],f["knowledge_lease"])
                        try:
                            if operation == "chunk_count":
                                uow.knowledge.activate_index(document_id=f["document"]["id"],version="test",artifact_path="test-only",artifact_sha256="a"*64,expected_chunk_count=1,lease=f["knowledge_lease"])
                            else:
                                point = CountInput(f["point"].window_start,f["video"]["recorded_end_at"],11,3)
                                uow.video.write_counts(f["analysis"]["id"],[point],f["video_lease"])
                        except Conflict: return "conflict"
                        return "unexpected success"
                future = pool.submit(stale_business)
                assert cached.wait(3)
                with store.write() as writer:
                    configure(writer)
                    if operation == "count_overlap": writer.video.write_counts(f["analysis"]["id"],[f["point"]],f["video_lease"])
                    else: writer.knowledge.write_chunks(f["index"]["id"],[{**f["chunk"],"chunk_no":1}],f["knowledge_lease"])
                    go.set()
                result = future.result(timeout=6)
                assert result == ({"inserted":0,"skipped":1} if operation == "chunk_duplicate" else "conflict"), (operation,result)
            passed("current_read_after_stale_snapshot_"+operation)
        with store.read() as uow:
            mysql_version = uow.session.scalar(text("SELECT VERSION()"))
            head = uow.session.scalar(text("SELECT version_num FROM alembic_version"))
        return {"status":"passed","package_version":__version__,"migration_head":head,"mysql_version":mysql_version,
            "check_count":len(checks),"checks":checks,"verified_at":datetime.now(timezone.utc).isoformat(),
            "scope":"synthetic_storage_competition_only","production_database_verified":False}
    finally: store.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--data-root",type=Path,required=True)
    args = parser.parse_args()
    result = verify(os.environ["TEST_DATABASE_URL"],args.data_root)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({k:result[k] for k in ("status","check_count","package_version","migration_head")}))
