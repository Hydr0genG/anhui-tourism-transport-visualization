"""C-run actual D knowledge HTTP/worker with installed C/E and fresh MySQL.

No provider replacement or D source changes. Fault injection is confined to the
new test schema and managed files. Does not prove receiving on D's own host.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from data_foundation import LeaseLost, UserCreate, create_store
from data_foundation.db.models import Job, KnowledgeIndex
from data_foundation.ingestion.real_dataset import import_real_dataset
from data_foundation.repositories import JobRepository
from data_foundation.seed import seed_catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d-root", type=Path, required=True)
    parser.add_argument("--d-commit", required=True)
    parser.add_argument("--e-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-socket-only-instance", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    url = os.environ.get("C_MAINLINE_DATABASE_URL", "")
    parsed = make_url(url)
    if parsed.drivername != "mysql+pymysql" or not re.fullmatch(r"cde_readiness_[a-z0-9_]+_test", parsed.database or ""):
        parser.error("Only a new dedicated cde_readiness_*_test MySQL schema is accepted")
    assert not args.output.exists(), "Never overwrite evidence"
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            socket_only = connection.scalar(text("SELECT @@skip_networking")) == 1
        assert not inspect(engine).get_table_names(), "Never clear an existing schema"
        if args.require_socket_only_instance:
            assert socket_only
    finally:
        engine.dispose()
    args.output.mkdir(parents=True)
    raw_root = (args.output / "raw").resolve()
    secret = secrets.token_hex(32)
    os.environ.update(DATABASE_URL=url, DATA_DIR=str(raw_root), D_JOB_STORE_BACKEND="c_store",
        CSRF_SECRET=secret, SECURE_COOKIES="false", RAG_INDEX_PATH=str(raw_root / "rag.json"),
        P2PNET_BUNDLE_DIR=str(raw_root / "unconfigured_p2p_bundle"),
        COUNTS_STORAGE_PATH=str(raw_root / "counts"), VIDEO_TMP_DIR=str(raw_root / "video_tmp"),
        DETECTOR_WEIGHTS_PATH=str(raw_root / "unused_weights"))
    result = subprocess.run([sys.executable, "-I", "-m", "alembic", "-c", str(root / "alembic.ini"),
        "upgrade", "head"], capture_output=True, text=True)
    assert result.returncode == 0, "Migration of new test schema failed"
    spec = importlib.util.spec_from_file_location("mainline_helpers", root / "examples/verify_cde_mainline_http.py")
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    packages = {"C": helpers.package(root / "release/anhui_data_foundation-0.2.1a2-py3-none-any.whl", "data_foundation"),
                "E": helpers.package(args.e_wheel, "intelligent_analysis")}
    from importlib.metadata import version
    packages["C"]["version"], packages["E"]["version"] = version("anhui-data-foundation"), version("shumei-intelligent-analysis")
    assert packages["C"]["version"] == "0.2.1a2" and packages["E"]["version"] == "0.6.1"
    sys.path.insert(0, str(args.d_root.resolve()))
    from app.core.providers import get_c_store
    from app.core.security import PasswordService
    from app.main import app
    from app.worker import DWorker
    from fastapi.testclient import TestClient
    import app as d_package
    assert Path(d_package.__file__).resolve().is_relative_to(args.d_root.resolve())
    source_hashes = {p.relative_to(args.d_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted((args.d_root / "app").rglob("*.py"))}
    store = create_store(url, data_root=raw_root)
    checks, samples = [], []
    known_secrets = [secret]
    def check(name, condition, **details):
        checks.append({"name": name, "status": "passed" if condition else "failed", **details})
        assert condition, name
    def body(response):
        data = response.json()
        return data.get("data", data)
    def sample(name, response):
        # Call only on answers/errors; never capture login/session responses.
        samples.append({"name": name, "status_code": response.status_code, "body": response.json()})
    report = {"scope": "C_run_unmodified_D_ASGI_knowledge_worker_on_MySQL_not_D_host_receiving",
        "D_commit": args.d_commit, "packages": packages, "checks": checks, "http_samples": samples,
        "D_source_sha256": source_hashes, "provider_replacement": False,
        "fault_injection_only_in_dedicated_test_schema": True, "real_public_knowledge_used": True,
        "synthetic_login_accounts": True, "D_upstream_modified": False, "E_source_modified": False,
        "business_DB_touched": False, "crawler_touched": False, "real_video_verified": False,
        "live_network_API": False, "mysql_network_listener_disabled": socket_only}
    try:
        seed_catalog(store)
        dataset = root / "real_data/2026-10-12-v2"
        import_real_dataset(store, dataset / "manifest.json")
        manifest = json.loads((dataset / "manifest.json").read_text())
        document = next(d for d in manifest["documents"] if d["id"] == "0c8e2020-2e0a-5842-bce9-d9193cf81095")
        password = secrets.token_urlsafe(24)
        known_secrets.append(password)
        with store.write() as uow:
            admin = uow.auth.create_user(UserCreate("synthetic-e061-admin", "SYNTHETIC acceptance admin",
                PasswordService.hash_password(password), "admin"))
        with TestClient(app, base_url="http://testserver", raise_server_exceptions=False) as client:
            adapter = get_c_store()
            question = {"message": "2025年春节黄山运营时间"}
            response = client.post("/api/v1/assistant/chat", json=question)
            check("anonymous_401", response.status_code == 401)
            login = client.post("/api/v1/auth/login", json={"username": "synthetic-e061-admin", "password": password})
            assert login.status_code == 200
            token = body(login)["csrf_token"]
            known_secrets.extend([token, *client.cookies.values()])
            headers = {"X-CSRF-Token": token}
            fields = {k: document[k] for k in ("title", "publisher", "source_url", "published_at", "permission_note", "valid_from", "valid_until")}
            response = client.post("/api/v1/knowledge/documents", headers=headers, data=fields,
                files={"file": ("historical-notice.txt", (dataset / document["text_path"]).read_bytes(), "text/plain")})
            check("20_real_notice_upload_enqueues", response.status_code == 202)
            accepted = body(response)
            with store.read() as uow:
                payload = copy.deepcopy(uow.session.get(Job, accepted["job_id"]).payload)
            check("E061_source_and_index_versions_independent", payload["version"] != payload["document_version"])
            original_ref = store.get_document_file_ref(accepted["document_id"])
            check("C_verified_original_bytes", Path(original_ref["file_path"]).read_bytes() == (dataset / document["text_path"]).read_bytes())
            check("D_worker_real_MySQL_index", DWorker(adapter).run_once()["status"] == "succeeded")
            response = client.post("/api/v1/assistant/chat", headers=headers, json=question)
            sample("19_real_historical_knowledge", response)
            answer = body(response)
            check("19_strict_loader_real_citations", response.status_code == 200 and answer["citations"]
                and any(c["id"] == accepted["document_id"] for c in answer["citations"]) and "历史资料" in answer["answer"])
            response = client.post("/api/v1/knowledge/reindex", headers=headers,
                json={"document_ids": [accepted["document_id"]], "reason": "C isolated E061 MySQL receiving"})
            check("22_reindex_accepted", response.status_code == 202)
            check("22_parent_and_child_processed", all(DWorker(adapter).run_once()["status"] == "succeeded" for _ in range(2)))
            check("22_rebuilt_answer_available", client.post("/api/v1/assistant/chat", headers=headers, json=question).status_code == 200)
            probe = adapter.create_job("knowledge_index", {**payload, "version": "rollback-" + uuid4().hex})
            with store.read() as uow:
                before = uow.knowledge.get_document(accepted["document_id"])["active_index_version"]
            original_complete = JobRepository.complete_job
            def fail_after_completion(repository, lease, refs, now):
                completed = original_complete(repository, lease, refs, now)
                if lease.job_id == probe:
                    raise RuntimeError("SYNTHETIC FAULT AFTER REAL MYSQL COMPLETION WRITE")
                return completed
            with patch.object(adapter, "activate_and_complete", wraps=adapter.activate_and_complete) as publication:
                with patch.object(JobRepository, "complete_job", fail_after_completion):
                    failed = DWorker(adapter).run_once()
                old_publication = publication.call_args.kwargs
            with store.read() as uow:
                check("D_activation_and_completion_roll_back_together", failed["status"] == "failed"
                    and uow.knowledge.get_document(accepted["document_id"])["active_index_version"] == before
                    and uow.jobs.get_job(probe)["status"] == "failed")
            with store.write() as uow:
                uow.jobs.retry_failed(probe, "SYNTHETIC RETRY", admin["id"], datetime.now(timezone.utc))
            try:
                adapter.activate_and_complete(**old_publication)
            except LeaseLost:
                check("old_lease_cannot_publish_after_retry", True)
            else:
                check("old_lease_cannot_publish_after_retry", False)
            check("new_attempt_publishes_successfully", DWorker(adapter).run_once()["status"] == "succeeded")
            restart_program = """import json, sys
sys.path.insert(0, sys.argv[1])
from app.services.e_bindings import configure_e_services
from intelligent_analysis.service import answer_message
assert configure_e_services()
answer = answer_message('2025年春节黄山运营时间')
print(json.dumps({'status': answer['status'], 'citation_ids': [c['id'] for c in answer['citations']]}))
"""
            restarted = subprocess.run([sys.executable, "-I", "-c", restart_program, str(args.d_root.resolve())],
                capture_output=True, text=True, timeout=60)
            restored = json.loads(restarted.stdout) if restarted.returncode == 0 else {}
            check("fresh_process_strict_loader_recovers_committed_MySQL_index", restarted.returncode == 0
                and restored.get("status") == "answered" and accepted["document_id"] in restored.get("citation_ids", []))
            with store.read() as uow:
                artifact = uow.knowledge.get_index_artifact_ref(accepted["document_id"])
            path = raw_root / artifact["relative_path"]
            original = path.read_bytes()
            try:
                path.write_bytes(b"SYNTHETIC ARTIFACT DAMAGE")
                response = client.post("/api/v1/assistant/chat", headers=headers, json=question)
                sample("damaged_artifact", response)
                check("damaged_artifact_503_no_stale_answer", response.status_code == 503)
            finally:
                path.write_bytes(original)
            check("repaired_artifact_restores_answer", client.post("/api/v1/assistant/chat", headers=headers, json=question).status_code == 200)
            # Correct SHA, wrong semantics: update only this test index reference,
            # leaving the C committed metadata/chunks as independent truth.
            for fault in ("text", "locator", "document_version", "index_version", "metadata"):
                mutated = copy.deepcopy(json.loads(original))
                item = mutated["documents"][accepted["document_id"]]
                if fault == "text": item["chunks"][0]["text"] += " SYNTHETIC TAMPER"
                elif fault == "locator": item["chunks"][0]["chunk_id"] += "-tamper"
                elif fault in {"document_version", "index_version"}: item[fault] += "-tamper"
                else: item["metadata"]["publisher"] += " SYNTHETIC TAMPER"
                altered = store.persist_file(source_id=original_ref["source_id"], original_name=fault + ".json",
                    stream=io.BytesIO(json.dumps(mutated, ensure_ascii=False).encode()))
                def set_reference(reference):
                    with store.write() as uow:
                        row = uow.session.get(KnowledgeIndex, artifact["index_id"])
                        row.artifact_path, row.artifact_sha256, row.artifact_byte_size = reference["relative_path"], reference["sha256"], reference["byte_size"]
                try:
                    set_reference(altered)
                    response = client.post("/api/v1/assistant/chat", headers=headers, json=question)
                    sample("correct_hash_wrong_" + fault, response)
                    check("correct_hash_wrong_" + fault + "_503", response.status_code == 503)
                finally:
                    set_reference(artifact)
                check("restore_after_" + fault, client.post("/api/v1/assistant/chat", headers=headers, json=question).status_code == 200)
            injection = {**question, "context": {"metric_tool": "client-cannot-inject"}}
            check("client_tool_injection_400", client.post("/api/v1/assistant/chat", headers=headers, json=injection).status_code == 400)
            check("missing_csrf_403", client.post("/api/v1/assistant/chat", json=question).status_code == 403)
            response = client.post("/api/v1/auth/logout", headers=headers)
            check("logout_revokes_session", response.status_code == 200 and client.post("/api/v1/assistant/chat", json=question).status_code == 401)
        forbidden = ("file_path", "artifact_path", "relative_path", "password_hash", "lease_token")
        check("HTTP_hides_internal_fields", all(key not in json.dumps(samples, ensure_ascii=False) for key in forbidden))
        check("D_snapshot_unchanged", source_hashes == {p.relative_to(args.d_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((args.d_root / "app").rglob("*.py"))})
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        store.dispose()
        serialized = json.dumps(report, ensure_ascii=False, indent=2, default=helpers.encode) + "\n"
        assert url not in serialized and all(value not in serialized for value in known_secrets)
        (args.output / "report.json").write_text(serialized)
        print(json.dumps({"status": report.get("status"), "checks": len(checks),
            "passed": sum(c["status"] == "passed" for c in checks)}))


if __name__ == "__main__":
    main()
