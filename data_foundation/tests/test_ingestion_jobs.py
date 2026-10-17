"""Synthetic local-CSV job acceptance; no crawler or business DB involved."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from uuid import uuid4

import pytest

from data_foundation import AssetCreate, JobCreate, LeaseLost
from data_foundation.db.models import IngestionRun, Observation, QualityIssue, Source
from data_foundation.ingestion import run_ingestion_once
from data_foundation.ingestion import jobs as ingestion_jobs
from data_foundation.repositories import JobRepository
from conftest import SCENIC_ID, SOURCE_ID


def prepare(store, tmp_path, *, dry_run=False, extra_row=""):
    store.data_root = tmp_path / "assets"
    content = ("entity_id,metric_code,period_start,period_end,grain,original_value,original_unit,standard_value,standard_unit,conversion_factor,source_locator,methodology_version\n"
        f"{SCENIC_ID},scenic_visits,2026-01-01,2026-01-31,month,125,人次,125,人次,1,合成CSV第2行,synthetic-1\n" + extra_row).encode()
    staged = store.persist_file(source_id=SOURCE_ID, original_name="synthetic.csv", stream=BytesIO(content))
    with store.write() as uow:
        asset = uow.provenance.register_asset(AssetCreate(source_id=SOURCE_ID,
            url="https://example.invalid/synthetic.csv", title="合成CSV", retrieved_at=datetime.now(timezone.utc),
            relative_path=staged["relative_path"], sha256=staged["sha256"], byte_size=staged["byte_size"],
            media_type="text/csv", usage_note="synthetic tests only"))
        uow.session.get(Source, SOURCE_ID).adapter_config = {"csv_asset_id": asset["id"]}
        job = uow.jobs.enqueue_or_get(JobCreate("ingestion", str(uuid4()), {"source_ids": [SOURCE_ID], "dry_run": dry_run}, "合成作业"), datetime.now(timezone.utc))
    return job, asset


def test_apply_and_repeat_import_are_atomic_idempotent_and_do_not_claim_e_jobs(store, tmp_path):
    job, _ = prepare(store, tmp_path)
    with store.write() as uow:
        other = uow.jobs.enqueue_or_get(JobCreate("knowledge_index", "other-type", {}, "test"), datetime.now(timezone.utc))
    result = run_ingestion_once(store, job_id=job["id"])
    assert result["status"] == "succeeded" and result["counts"]["imported"] == 1
    assert run_ingestion_once(store, job_id=job["id"]) is None
    with store.write() as uow:
        assert uow.jobs.get_job(other["id"])["status"] == "queued"
        finished = uow.jobs.get_job(job["id"])
        assert finished["status"] == "succeeded" and finished["imported_count"] == 1
        assert uow.session.query(IngestionRun).one().job_id == job["id"]
        again = uow.jobs.enqueue_or_get(JobCreate("ingestion", str(uuid4()), job["payload"], "repeat"), datetime.now(timezone.utc))
    result = run_ingestion_once(store, job_id=again["id"])
    assert result["counts"]["skipped"] == 1 and result["counts"]["imported"] == 0
    with store.read() as uow:
        assert uow.session.query(Observation).count() == 1


def test_dry_run_publishes_preview_and_audit_but_no_observation(store, tmp_path):
    job, _ = prepare(store, tmp_path, dry_run=True)
    result = run_ingestion_once(store, job_id=job["id"])
    assert result["dry_run"] and result["counts"]["imported"] == 1  # preview count
    with store.read() as uow:
        assert uow.session.query(Observation).count() == 0
        assert uow.jobs.get_job(job["id"])["imported_count"] == 0
        assert uow.session.query(IngestionRun).one().dry_run


def test_completion_then_failure_rolls_back_import_and_completion_together(store, tmp_path, monkeypatch):
    job, _ = prepare(store, tmp_path)
    original = JobRepository.complete_job
    def fail_after_completion(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("synthetic failure after completing")
    monkeypatch.setattr(JobRepository, "complete_job", fail_after_completion)
    assert run_ingestion_once(store, job_id=job["id"])["status"] == "failed"
    with store.read() as uow:
        assert uow.jobs.get_job(job["id"])["status"] == "failed"
        assert uow.session.query(Observation).count() == uow.session.query(IngestionRun).count() == 0


@pytest.mark.parametrize("case", ["content", "missing", "metadata", "unsupported", "payload_path"])
def test_untrusted_or_changed_input_is_never_published(store, tmp_path, monkeypatch, case):
    job, asset = prepare(store, tmp_path)
    path = store.data_root / asset["relative_path"]
    if case == "content":
        path.write_bytes(b"tampered")
    elif case == "missing":
        path.unlink()
    elif case == "unsupported":
        with store.write() as uow:
            uow.session.get(Source, SOURCE_ID).adapter_config = {}
    elif case == "payload_path":
        from data_foundation.db.models import Job
        with store.write() as uow:
            uow.session.get(Job, job["id"]).payload = {**job["payload"], "csv_path": "/arbitrary/path"}
    else:
        original = ingestion_jobs._prepare
        def change_after_prepare(*args):
            result = original(*args)
            with store.write() as uow:
                uow.session.get(Source, SOURCE_ID).adapter_config = {"csv_asset_id": str(uuid4())}
            return result
        monkeypatch.setattr(ingestion_jobs, "_prepare", change_after_prepare)
    assert run_ingestion_once(store, job_id=job["id"])["status"] == "failed"
    with store.read() as uow:
        assert uow.session.query(Observation).count() == 0
        assert uow.session.query(IngestionRun).count() == 0


def test_old_worker_cannot_publish_or_fail_new_attempt(store, tmp_path, monkeypatch):
    job, _ = prepare(store, tmp_path)
    original = ingestion_jobs._prepare
    def recover_after_prepare(*args):
        result = original(*args)
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        with store.write() as uow:
            assert uow.jobs.recover_expired(future)["requeued"] == 1
            newer = uow.jobs.claim_job("new-worker", future, 600, job_type="ingestion", job_id=job["id"])
            assert newer["job"]["attempt_count"] == 2
        return result
    monkeypatch.setattr(ingestion_jobs, "_prepare", recover_after_prepare)
    with pytest.raises(LeaseLost):
        run_ingestion_once(store, job_id=job["id"], lease_seconds=1)
    with store.read() as uow:
        assert uow.jobs.get_job(job["id"])["status"] == "running"
        assert uow.session.query(Observation).count() == 0


def test_invalid_row_is_a_quality_issue_with_counts_not_a_fabricated_zero(store, tmp_path):
    job, _ = prepare(store, tmp_path, extra_row=f"{SCENIC_ID},scenic_visits,invalid,2026-02-28,month,0,人次,0,人次,1,坏行,synthetic-1\n")
    result = run_ingestion_once(store, job_id=job["id"])
    assert result["status"] == "succeeded"
    assert result["counts"]["processed"] == 2 and result["counts"]["quarantined"] == 1
    with store.read() as uow:
        assert uow.session.query(Observation).count() == uow.session.query(QualityIssue).count() == 1
