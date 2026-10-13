"""独占空 MySQL 测试库中的恢复/续约/发布竞争验收。

TEST_DATABASE_URL 仅允许 mysql+pymysql、库名 _test、无表。脚本负责迁移，
全部为合成任务/文档。虚拟时间用于确定到期边界，不等待真实租约到期。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from threading import Event
from time import monotonic
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.engine import make_url

from data_foundation import AssetCreate, JobCreate, JobLease, LeaseLost, SourceCreate, __version__, create_store
from data_foundation.db.models import Job, JobAttempt, KnowledgeChunk
from data_foundation.seed import seed_catalog
from de_handoff_smoke import migrate


def verify(database_url: str) -> dict:
    parsed = make_url(database_url)
    if parsed.drivername != "mysql+pymysql" or not (parsed.database or "").endswith("_test"):
        raise ValueError("仅允许名称以 _test 结尾的独占 MySQL 测试库")
    engine = create_engine(database_url)
    try:
        if inspect(engine).get_table_names():
            raise ValueError("测试库必须为空；不清理或复用已有数据库")
    finally:
        engine.dispose()
    migrate(database_url)
    store = create_store(database_url)
    checks = []
    # 默认无 now 的业务租约检查使用实时时钟；虚拟开始时间在未来，
    # 从而在测试期间不会因执行耗时随机越过期限。
    start = datetime.now(timezone.utc)+timedelta(minutes=5)
    due = start+timedelta(seconds=1)

    def connection(uow):
        uow.session.execute(text("SET SESSION innodb_lock_wait_timeout = 3"))
        return uow.session.scalar(text("SELECT CONNECTION_ID()"))

    def running():
        with store.write() as uow:
            job = uow.jobs.enqueue_or_get(JobCreate("knowledge_index",uuid4().hex,{"synthetic":True},"仅恢复竞争测试"),start)
            claimed = uow.jobs.claim_job("worker-original",start,1,job_id=job["id"])
        return JobLease(**claimed["lease"])

    def state(lease, status, outcome):
        with store.read() as uow:
            job = uow.session.get(Job,lease.job_id)
            attempt = uow.session.scalar(select(JobAttempt).where(JobAttempt.job_id==lease.job_id,JobAttempt.attempt_no==lease.attempt_no))
            assert job.status == status and attempt.outcome == outcome

    def fixture(lease):
        with store.write() as uow:
            asset = uow.provenance.register_asset(AssetCreate(source_id=source_id,url="https://example.invalid/recovery/"+uuid4().hex,
                title="合成原件",retrieved_at=start,relative_path="test-only/not-for-replay",sha256=uuid4().hex*2,
                media_type="text/plain",byte_size=0,usage_note="仅合成存储验收"))
            document = uow.knowledge.register_document({"source_id":source_id,"asset_id":asset["id"],"title":"合成恢复文档",
                "publisher":"测试","source_url":asset["url"],"published_at":start,"permission_note":"仅合成验收","document_version":"test"})
            index = uow.knowledge.begin_index(document_id=document["id"],version="test",job_id=lease.job_id,
                parser_version="test",embedding_model_version="test",lease=lease)
            uow.knowledge.write_chunks(index["id"],[{"chunk_no":0,"text":"已提交片段","locator":{"page":1}}],lease)
        return document["id"],index["id"]

    def publish(uow, document_id, lease):
        uow.knowledge.activate_index(document_id=document_id,version="test",artifact_path="test-only",
            artifact_sha256="a"*64,expected_chunk_count=1,lease=lease)

    def passed(name, **details):
        checks.append({"name":name,"status":"passed",**details})

    try:
        seed_catalog(store)
        with store.write() as uow:
            source_id = uow.provenance.register_source(SourceCreate(id=str(uuid4()),publisher="合成恢复验收",title="仅测试来源",
                url="https://example.invalid/recovery-source",usage_note="仅合成测试，不是真实原件",update_frequency="irregular",
                verification_status="verified"))["id"]
        with store.read() as uow:
            version = uow.session.scalar(text("SELECT VERSION()"))
        lease = running()
        with store.write() as first:
            first_id = connection(first)
            assert first.jobs.recover_expired(due) == {"requeued":1,"failed":0}
            with store.write() as second:
                assert connection(second) != first_id
                assert second.jobs.recover_expired(due) == {"requeued":0,"failed":0}
        state(lease,"queued","expired")
        with store.read() as uow:
            assert uow.session.scalar(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id==lease.job_id)) == 1
        passed("two_recoverers_do_not_duplicate_attempt_or_requeue")

        lease = running()
        with store.write() as holder:
            holder_id = connection(holder)
            holder.session.scalar(select(Job).where(Job.id==lease.job_id).with_for_update())
            with store.write() as recovery:
                assert connection(recovery) != holder_id
                begun = monotonic()
                assert recovery.jobs.recover_expired(due) == {"requeued":0,"failed":0}
                assert monotonic()-begun < 2
        with store.write() as recovery:
            assert recovery.jobs.recover_expired(due) == {"requeued":1,"failed":0}
        passed("locked_job_is_skipped_and_recovered_after_unlock")

        # 读取到期候选后，另一连接先完成有效续约；锁后必须读到新期限。
        lease = running()
        renewed = []
        def renew_after_candidates(conn,cursor,statement,parameters,context,executemany):
            if not renewed and statement.lstrip().startswith("SELECT jobs.id") and "JOIN job_attempts" in statement:
                renewed.append(True)
                with store.write() as heartbeat:
                    assert connection(heartbeat) != conn.scalar(text("SELECT CONNECTION_ID()"))
                    heartbeat.jobs.heartbeat(lease,due-timedelta(microseconds=1),60)
        event.listen(store.engine,"after_cursor_execute",renew_after_candidates)
        try:
            with store.write() as recovery:
                assert recovery.jobs.recover_expired(due) == {"requeued":0,"failed":0}
            assert renewed
        finally:
            event.remove(store.engine,"after_cursor_execute",renew_after_candidates)
        state(lease,"running",None)
        passed("heartbeat_commits_between_candidate_read_and_lock_recheck")

        # 发布者先持有 Job/Attempt 锁，恢复者跳过；原发布与完成同事务提交。
        lease = running()
        document_id,index_id = fixture(lease)
        with store.write() as publisher:
            publisher_id = connection(publisher)
            publish(publisher,document_id,lease)
            with store.write() as recovery:
                assert connection(recovery) != publisher_id
                assert recovery.jobs.recover_expired(due) == {"requeued":0,"failed":0}
            publisher.jobs.complete_job(lease,{"document_id":document_id},start)
        state(lease,"succeeded","succeeded")
        with store.write() as recovery:
            assert recovery.jobs.recover_expired(due) == {"requeued":0,"failed":0}
        passed("publisher_lock_prevents_requeue_and_commits_atomically")

        # 先缓存旧对象，再让恢复者提交；写检查必须等待行锁并刷新缓存。
        lease = running()
        document_id,index_id = fixture(lease)
        cached,proceed,attempting,finished = Event(),Event(),Event(),Event()
        def old_publisher():
            with store.write() as uow:
                connection(uow)
                uow.session.get(Job,lease.job_id)
                uow.session.scalar(select(JobAttempt).where(JobAttempt.job_id==lease.job_id))
                cached.set()
                assert proceed.wait(5)
                attempting.set()
                try:
                    publish(uow,document_id,lease)
                except LeaseLost:
                    finished.set()
                    return "lease_lost"
                finished.set()
                return "unexpected_publish"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(old_publisher)
            assert cached.wait(5)
            try:
                with store.write() as recovery:
                    connection(recovery)
                    assert recovery.jobs.recover_expired(due) == {"requeued":1,"failed":0}
                    proceed.set()
                    assert attempting.wait(5)
                    assert not finished.wait(0.1), "旧发布必须等恢复事务释放 Job 锁"
            finally:
                proceed.set()
            assert future.result(timeout=5) == "lease_lost"
        state(lease,"queued","expired")
        with store.read() as uow:
            assert uow.knowledge.get_document(document_id)["index_status"] == "pending"
            assert uow.session.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.index_id==index_id)) == 1
        with store.write() as uow:
            for operation in (lambda:uow.jobs.heartbeat(lease,due),lambda:uow.jobs.complete_job(lease,{},due),lambda:uow.jobs.fail_job(lease,"old",due)):
                try:operation()
                except LeaseLost:pass
                else:raise AssertionError("恢复后旧租约仍被接受")
            new = JobLease(**uow.jobs.claim_job("new-worker",due,60,job_id=lease.job_id)["lease"])
            publish(uow,document_id,new)
            uow.jobs.complete_job(new,{"document_id":document_id},due)
        state(new,"succeeded","succeeded")
        passed("recovery_wins_stale_cached_publisher_waits_then_is_rejected")

        lease = running()
        class RollbackProbe(Exception):pass
        try:
            with store.write() as uow:
                assert uow.jobs.recover_expired(due) == {"requeued":1,"failed":0}
                raise RollbackProbe()
        except RollbackProbe:pass
        state(lease,"running",None)
        with store.write() as uow:
            assert uow.jobs.recover_expired(due,max_attempts=1) == {"requeued":0,"failed":1}
            assert uow.jobs.recover_expired(due) == {"requeued":0,"failed":0}
            assert uow.jobs.claim_job("new",due,job_id=lease.job_id) is None
        state(lease,"failed","expired")
        passed("recovery_rollback_attempt_cap_and_repeat_are_consistent")
        return {"package_version":__version__,"status":"passed","scope":"isolated_mysql_synthetic_recovery_competition",
            "database_version":version,"migration_head":"0005","checks":checks,"check_count":len(checks),
            "verified_at":datetime.now(timezone.utc).isoformat(),"production_database_verified":False,
            "limitations":["虚拟时间验证操作校验时刻及事务顺序，不承诺提交瞬间重新检查实时时钟。",
                "现有队列索引及锁范围限制保留；未验证高并发吞吐或D/E业务重试幂等。"]}
    finally:
        store.dispose()


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report",type=Path,required=True)
    args=parser.parse_args()
    try:
        url=os.environ.get("TEST_DATABASE_URL")
        if not url:raise ValueError("请配置 TEST_DATABASE_URL")
        report=verify(url)
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({"status":report["status"],"package_version":report["package_version"],"check_count":report["check_count"]}))
    except Exception as exc:
        message=str(exc) if isinstance(exc,ValueError) else "验收失败；检查独占测试环境，未标记通过。"
        print(json.dumps({"status":"failed","error_type":type(exc).__name__,"message":message},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1)
