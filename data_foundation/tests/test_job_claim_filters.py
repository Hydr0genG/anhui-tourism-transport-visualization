from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from data_foundation import InvalidArgument, JobCreate, JobLease
from data_foundation.db.models import Job, JobAttempt


def enqueue(store, kinds=("video_analysis", "knowledge_index", "knowledge_index")):
    now = datetime.now(timezone.utc)
    ids = []
    with store.write() as uow:
        for i, kind in enumerate(kinds):
            item = uow.jobs.enqueue_or_get(JobCreate(kind, f"filter-{i}", {}, "仅测试"), now)
            ids.append(item["id"])
            # 固定排序，不依赖系统时钟或 UUID 随机顺序。
            uow.session.get(Job, item["id"]).created_at = now.replace(tzinfo=None) + timedelta(seconds=i)
    return now, ids


def assert_unchanged(store, ids):
    with store.read() as uow:
        for job_id in ids:
            row = uow.session.get(Job, job_id)
            assert row.status == "queued" and row.attempt_count == 0 and row.started_at is None
        assert uow.session.scalar(select(func.count()).select_from(JobAttempt)) == 0


def test_legacy_claim_keeps_created_at_then_id_order(store):
    now, ids = enqueue(store)
    with store.write() as uow:
        # 同创建时刻时，以 ID 确定顺序。
        for job_id in ids:
            uow.session.get(Job, job_id).created_at = now.replace(tzinfo=None)
    with store.write() as uow:
        for expected in sorted(ids):
            result = uow.jobs.claim_job("legacy-worker", now, 120)
            assert result["job"]["id"] == expected
            assert set(result) == {"job", "lease"}
        assert uow.jobs.claim_job("legacy-worker", now) is None


def test_type_filter_claims_oldest_matching_job_and_preserves_other_types(store):
    now, ids = enqueue(store)
    with store.write() as uow:
        result = uow.jobs.claim_job("worker", now, job_type="knowledge_index")
        assert result["job"]["id"] == ids[1]
        assert result["job"]["attempt_count"] == 1
        assert uow.session.get(Job, ids[0]).status == "queued"
        lease = JobLease(**result["lease"])
        attempt = uow.jobs.ensure_lease(lease, now)
        assert attempt.worker_id == "worker"
        assert attempt.lease_expires_at == now.replace(tzinfo=None) + timedelta(seconds=120)


@pytest.mark.parametrize("combined", [False, True])
def test_id_filter_can_select_later_job(store, combined):
    now, ids = enqueue(store)
    kwargs = {"job_id": ids[2]}
    if combined:
        kwargs["job_type"] = "knowledge_index"
    with store.write() as uow:
        result = uow.jobs.claim_job("worker", now, **kwargs)
        assert result["job"]["id"] == ids[2]
        assert uow.session.get(Job, ids[0]).status == uow.session.get(Job, ids[1]).status == "queued"


@pytest.mark.parametrize("case", ["unknown_type", "unknown_id", "mismatch", "exact_type", "exact_id", "type_case", "type_trailing", "id_trailing"])
def test_no_match_has_no_side_effects_or_fallback(store, case):
    now, ids = enqueue(store)
    options = {
        "unknown_type": {"job_type": "unknown"}, "unknown_id": {"job_id": "missing"},
        "mismatch": {"job_id": ids[0], "job_type": "knowledge_index"},
        "exact_type": {"job_type": " knowledge_index "}, "exact_id": {"job_id": f" {ids[0]} "},
        "type_case": {"job_type": "Knowledge_Index"}, "type_trailing": {"job_type": "knowledge_index "},
        "id_trailing": {"job_id": ids[0] + " "},
    }
    with store.write() as uow:
        assert uow.jobs.claim_job("worker", now, **options[case]) is None
    assert_unchanged(store, ids)


@pytest.mark.parametrize("field", ["job_type", "job_id"])
@pytest.mark.parametrize("value", ["", " \t\n", 0, False, [], {}])
def test_invalid_filters_fail_before_any_task_mutation(store, field, value):
    now, ids = enqueue(store)
    with store.write() as uow:
        with pytest.raises(InvalidArgument):
            uow.jobs.claim_job("worker", now, **{field: value})
    assert_unchanged(store, ids)


@pytest.mark.parametrize("state", ["future", "running", "succeeded", "failed"])
def test_filters_do_not_bypass_status_or_available_time(store, state):
    now, ids = enqueue(store)
    with store.write() as uow:
        target = uow.session.get(Job, ids[1])
        if state == "future":
            target.available_at = now.replace(tzinfo=None) + timedelta(microseconds=1)
        else:
            target.status = state
    with store.write() as uow:
        assert uow.jobs.claim_job("worker", now, job_type="knowledge_index", job_id=ids[1]) is None
        assert uow.session.get(Job, ids[0]).status == uow.session.get(Job, ids[2]).status == "queued"
        assert uow.session.scalar(select(func.count()).select_from(JobAttempt)) == 0


def test_rollback_restores_queue_and_attempts(store):
    now, ids = enqueue(store)
    with pytest.raises(RuntimeError):
        with store.write() as uow:
            assert uow.jobs.claim_job("worker", now, job_id=ids[1])["job"]["id"] == ids[1]
            raise RuntimeError("模拟领取事务回滚")
    assert_unchanged(store, ids)
    with store.write() as uow:
        assert uow.jobs.claim_job("worker", now, job_id=ids[1])["job"]["attempt_count"] == 1
