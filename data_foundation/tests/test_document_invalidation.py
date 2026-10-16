from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

import pytest

from data_foundation import AssetCreate, InvalidPermission, JobCreate, JobLease, UserCreate
from data_foundation.repositories import JobRepository
from conftest import SOURCE_ID


def setup_document(store, tmp_path, role="admin"):
    now = datetime.now(timezone.utc)
    store.data_root = tmp_path
    original = store.persist_file(source_id=SOURCE_ID,original_name="notice.txt",stream=BytesIO(b"synthetic original"))
    artifact = store.persist_file(source_id=SOURCE_ID,original_name="index.json",stream=BytesIO(b"synthetic index, C is format neutral"))
    with store.write() as uow:
        operator = uow.auth.create_user(UserCreate("synthetic-"+uuid4().hex,"test","NOT_A_REAL_PASSWORD",role))
        asset = uow.provenance.register_asset(AssetCreate(SOURCE_ID,"https://example.invalid/doc","synthetic",now,
            original["relative_path"],original["sha256"],"text/plain",original["byte_size"],"synthetic test only"))
        doc = uow.knowledge.register_document({"source_id":SOURCE_ID,"asset_id":asset["id"],"title":"synthetic",
            "publisher":"test","source_url":"https://example.invalid/doc","published_at":now,
            "permission_note":"synthetic","document_version":"registered-opaque-source-version"})
        job = uow.jobs.enqueue_or_get(JobCreate("knowledge_index",uuid4().hex,{},"synthetic"),now)
        lease = JobLease(**uow.jobs.claim_job("test",now,600,job_id=job["id"])["lease"])
        idx = uow.knowledge.begin_index(document_id=doc["id"],version="v1",job_id=job["id"],parser_version="test",embedding_model_version="test",lease=lease)
        uow.knowledge.write_chunks(idx["id"],[{"chunk_no":0,"text":"synthetic","locator":{"test":True}}],lease)
        uow.knowledge.activate_index(document_id=doc["id"],version="v1",artifact_path=artifact["relative_path"],artifact_sha256=artifact["sha256"],expected_chunk_count=1,lease=lease)
        uow.jobs.complete_job(lease,{},now)
        job = uow.jobs.enqueue_or_get(JobCreate("knowledge_invalidate",uuid4().hex,{"document_id":doc["id"]},"explicit admin action",requested_by=operator["id"]),now)
        lease = JobLease(**uow.jobs.claim_job("test",now,600,job_id=job["id"])["lease"])
    return doc,operator,job,lease


def test_explicit_admin_invalidation_preserves_artifact_and_last_version(store,tmp_path):
    doc,operator,job,lease = setup_document(store,tmp_path)
    reference = store.get_document_file_ref(doc["id"])
    assert reference["document_version"] == "registered-opaque-source-version"
    with store.write() as uow:
        result = uow.knowledge.invalidate_document(document_id=doc["id"],reason="operator confirmed corrupt index",
            operator_id=operator["id"],lease=lease)
        assert result["previous_index_version"] == "v1"
    with store.read() as uow:
        assert uow.knowledge.get_document(doc["id"])["active_index_version"] == "v1"
        assert uow.knowledge.get_index_artifact_ref(doc["id"])["status"] == "index_not_ready"
        assert uow.knowledge.get_chunks(doc["id"])["items"] == []
        assert uow.knowledge.get_index_artifact_ref(doc["id"],"v1")["status"] == "ready"
        assert uow.jobs.get_job(job["id"])["result_refs"]["reason"] == "operator confirmed corrupt index"


def test_non_admin_cannot_invalidate(store,tmp_path):
    doc,operator,job,lease = setup_document(store,tmp_path,"viewer")
    with store.write() as uow:
        with pytest.raises(InvalidPermission):
            uow.knowledge.invalidate_document(document_id=doc["id"],reason="not allowed",operator_id=operator["id"],lease=lease)
    with store.read() as uow:
        assert uow.knowledge.get_document(doc["id"])["index_status"] == "ready"
        assert uow.jobs.get_job(job["id"])["status"] == "running"


def test_invalidation_and_completion_failure_roll_back_together(store,tmp_path):
    doc,operator,job,lease = setup_document(store,tmp_path)
    original = JobRepository.complete_job
    def fail(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise RuntimeError("after complete")
    with store.write() as uow:
        with patch.object(JobRepository,"complete_job",fail),pytest.raises(RuntimeError):
            uow.knowledge.invalidate_document(document_id=doc["id"],reason="test rollback",operator_id=operator["id"],lease=lease)
    with store.read() as uow:
        assert uow.knowledge.get_document(doc["id"])["index_status"] == "ready"
        assert uow.jobs.get_job(job["id"])["status"] == "running"
