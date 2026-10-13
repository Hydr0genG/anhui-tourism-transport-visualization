"""在独占空 MySQL 测试库验证领取筛选和两个连接的交错事务。

只接受 TEST_DATABASE_URL（mysql+pymysql、库名以 _test 结尾、无表）。
脚本执行迁移，使用合成任务，不访问业务库或网络资料。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from time import monotonic
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url

from data_foundation import JobCreate, JobLease, LeaseLost, __version__, create_store
from data_foundation.db.models import Job, JobAttempt
from de_handoff_smoke import migrate


def verify(database_url: str) -> dict:
    parsed = make_url(database_url)
    if parsed.drivername != "mysql+pymysql" or not (parsed.database or "").endswith("_test"):
        raise ValueError("只接受名称以 _test 结尾的独占 MySQL 测试库")
    engine = create_engine(database_url)
    try:
        if inspect(engine).get_table_names():
            raise ValueError("验收库必须为空；脚本不清理已有数据，请另建测试库")
    finally:
        engine.dispose()
    migrate(database_url)
    store = create_store(database_url)
    checks = []
    now = datetime.now(timezone.utc)

    def enqueue(kind):
        with store.write() as uow:
            return uow.jobs.enqueue_or_get(JobCreate(kind, str(uuid4()), {"synthetic": True}, "领取筛选验收，仅测试库"), now)["id"]

    def connection_id(uow):
        uow.session.execute(text("SET SESSION innodb_lock_wait_timeout = 3"))
        return uow.session.scalar(text("SELECT CONNECTION_ID()"))

    def check(name, **details):
        checks.append({"name": name, "status": "passed", **details})

    def counts(job_id, status, attempts):
        with store.read() as uow:
            job = uow.session.get(Job, job_id)
            assert job.status == status and job.attempt_count == attempts
            assert uow.session.scalar(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id == job_id)) == attempts

    try:
        with store.read() as uow:
            version = uow.session.scalar(text("SELECT VERSION()"))
            migration = uow.session.scalar(text("SELECT version_num FROM alembic_version"))

        target, other = enqueue("knowledge_index"), enqueue("video_analysis")
        with store.write() as first:
            first_id = connection_id(first)
            claimed = first.jobs.claim_job("worker-a", now, job_type="knowledge_index", job_id=target)
            assert claimed["job"]["id"] == target
            with store.write() as second:
                second_id = connection_id(second)
                assert second_id != first_id
                started = monotonic()
                assert second.jobs.claim_job("worker-b", now, job_id=target) is None
                elapsed = monotonic()-started
                assert elapsed < 2, "被锁任务的筛选查询应立即跳过"
        with store.write() as uow:
            assert uow.jobs.claim_job("worker-c", now, job_id=target) is None
        counts(target, "running", 1)
        counts(other, "queued", 0)
        check("same_job_claimed_once_and_locked_id_has_no_fallback", two_distinct_connections=True, skip_elapsed_seconds=round(elapsed, 4))

        # 明确通过主键只锁第一个匹配行，再验证类型查询能跳过它。
        # 无新增排序索引时，类型领取的 filesort 本身可能锁多个候选行；
        # 不以 LIMIT 1 推断第一连接只锁一行。
        kind = "test_claim_skip_type"
        a, b = enqueue(kind), enqueue(kind)
        with store.write() as uow:
            uow.session.get(Job, a).created_at = now.replace(tzinfo=None)
            uow.session.get(Job, b).created_at = now.replace(tzinfo=None)+timedelta(seconds=1)
        with store.write() as first:
            first_id = connection_id(first)
            assert first.session.scalar(select(Job).where(Job.id == a).with_for_update()).id == a
            with store.write() as second:
                assert connection_id(second) != first_id
                assert second.jobs.claim_job("worker-b", now, job_type=kind)["job"]["id"] == b
        counts(a, "queued", 0); counts(b, "running", 1)
        counts(other, "queued", 0)
        check("type_filter_skips_locked_matching_job")

        rollback_id = enqueue("test_claim_rollback")
        class RollbackProbe(Exception):
            pass
        try:
            with store.write() as first:
                first_id = connection_id(first)
                result = first.jobs.claim_job("worker-a", now, job_id=rollback_id)
                old_lease = JobLease(**result["lease"])
                with store.write() as second:
                    assert connection_id(second) != first_id
                    assert second.jobs.claim_job("worker-b", now, job_id=rollback_id) is None
                raise RollbackProbe()
        except RollbackProbe:
            pass
        counts(rollback_id, "queued", 0)
        with store.write() as uow:
            try:
                uow.jobs.ensure_lease(old_lease, now)
            except LeaseLost:
                pass
            else:
                raise AssertionError("回滚租约不可继续使用")
            result = uow.jobs.claim_job("worker-c", now, job_id=rollback_id)
            assert result["job"]["attempt_count"] == 1 and result["lease"]["lease_token"] != old_lease.lease_token
        counts(rollback_id, "running", 1)
        check("rollback_removes_attempt_and_allows_fresh_claim")

        before = []
        with store.read() as uow:
            before = [(j.id,j.status,j.attempt_count) for j in uow.session.scalars(select(Job).order_by(Job.id))]
            attempt_count = uow.session.scalar(select(func.count()).select_from(JobAttempt))
        with store.write() as uow:
            assert uow.jobs.claim_job("worker", now, job_type="video_analysis", job_id=rollback_id) is None
            assert uow.jobs.claim_job("worker", now, job_type="Video_Analysis") is None
            assert uow.jobs.claim_job("worker", now, job_type="video_analysis ") is None
            assert uow.jobs.claim_job("worker", now, job_id=other+" ") is None
        with store.read() as uow:
            assert before == [(j.id,j.status,j.attempt_count) for j in uow.session.scalars(select(Job).order_by(Job.id))]
            assert attempt_count == uow.session.scalar(select(func.count()).select_from(JobAttempt))
        check("intersection_mismatch_has_no_side_effects")
        check("case_and_trailing_spaces_match_exactly")
        return {"package_version": __version__, "status":"passed", "scope":"isolated_mysql_synthetic_claim_transactions",
                "database_version":version,"migration_head":migration,"checks":checks,"check_count":len(checks),
                "limitations":["沿用现有索引及排序，MySQL领取查询可能锁多个扫描候选行；本报告不证明每次只锁一行，也不证明高并发吞吐。"],
                "verified_at":datetime.now(timezone.utc).isoformat(),"production_database_verified":False}
    finally:
        store.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            raise ValueError("请配置 TEST_DATABASE_URL")
        report = verify(url)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({"status":report["status"],"package_version":report["package_version"],"check_count":report["check_count"]}))
    except Exception as exc:
        # 驱动异常可能带连接信息；报告只保留类别和自有安全提示。
        message = str(exc) if isinstance(exc, ValueError) else "验收失败，请在独占测试环境检查；未标记通过。"
        print(json.dumps({"status":"failed","error_type":type(exc).__name__,"message":message},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1)
