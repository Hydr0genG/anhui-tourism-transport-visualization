"""C-run D ASGI HTTP -> C MySQL worker -> D HTTP acceptance.

Uses unmodified D snapshot and installed C/E wheels, real existing C data,
and synthetic isolated login accounts. No live network API or business DB.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from zipfile import ZipFile

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from data_foundation import UserCreate, create_store
from data_foundation.db import Base
from data_foundation.ingestion import run_ingestion_once
from data_foundation.ingestion.real_dataset import import_real_dataset
from data_foundation.seed import seed_catalog


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def package(wheel, module):
    folder = Path(importlib.import_module(module).__file__).resolve().parent
    assert folder.is_relative_to(Path(sys.prefix)) and "site-packages" in folder.parts
    files = 0
    with ZipFile(wheel) as zipped:
        for name in zipped.namelist():
            if name.startswith(module + "/") and name.endswith((".py", ".json")):
                assert (folder / Path(name).relative_to(module)).read_bytes() == zipped.read(name)
                files += 1
    return {"wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "matching_files": files}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d-root", type=Path, required=True)
    parser.add_argument("--d-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--e-wheel", type=Path, help="Official E 0.6.0 or 0.6.1 wheel matching the installed runtime")
    parser.add_argument("--candidate-patch", type=Path, help="Only for a disposable D candidate copy; never label it as unmodified")
    parser.add_argument("--require-socket-only-instance", action="store_true", help="C local acceptance additionally requires MySQL skip_networking")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    url = os.environ.get("C_MAINLINE_DATABASE_URL", "")
    parsed = make_url(url)
    if parsed.drivername != "mysql+pymysql" or not re.fullmatch(r"cde_readiness_[a-z0-9_]+_test", parsed.database or ""):
        parser.error("Only a new dedicated cde_readiness_*_test schema is accepted")
    assert not args.output.exists(), "Never overwrite evidence"
    engine = create_engine(url)
    with engine.connect() as connection:
        socket_only = connection.scalar(text("SELECT @@skip_networking")) == 1
        if args.require_socket_only_instance:
            assert socket_only, "This run requires a socket-only test instance"
    assert not inspect(engine).get_table_names(), "Never clear an existing schema"
    engine.dispose()
    args.output.mkdir(parents=True)
    raw_root = args.output / "raw"
    settings = {"DATABASE_URL": url, "DATA_DIR": str(raw_root.resolve()), "D_JOB_STORE_BACKEND": "c_store",
        "CSRF_SECRET": secrets.token_hex(32), "SECURE_COOKIES": "false",
        "RAG_INDEX_PATH": str((raw_root / "rag.json").resolve()),
        "DETECTOR_WEIGHTS_PATH": str((raw_root / "weights").resolve()),
        "VIDEO_TMP_DIR": str((raw_root / "video_tmp").resolve()),
        "COUNTS_STORAGE_PATH": str((raw_root / "counts").resolve()),
        "P2PNET_BUNDLE_DIR": str((raw_root / "unconfigured_p2p_bundle").resolve())}
    os.environ.update(settings)
    command = subprocess.run([sys.executable, "-I", "-m", "alembic", "-c", str(root / "alembic.ini"), "upgrade", "head"],
        capture_output=True, text=True, env=os.environ.copy())
    assert command.returncode == 0, "Isolated migration failed"
    packages = {"C": package(root / "release/anhui_data_foundation-0.2.1a2-py3-none-any.whl", "data_foundation"),
        "E": package(args.e_wheel or root / "acceptance/C-DE-readiness-2026-10-16/upstream-e060/shumei_intelligent_analysis-0.6.0-py3-none-any.whl", "intelligent_analysis")}
    packages["C"]["version"] = importlib.metadata.version("anhui-data-foundation")
    packages["E"]["version"] = importlib.metadata.version("shumei-intelligent-analysis")
    assert packages["C"]["version"] == "0.2.1a2" and packages["E"]["version"] in {"0.6.0", "0.6.1"}
    sys.path.insert(0, str(args.d_root.resolve()))
    from app.core.security import PasswordService
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.providers import get_c_store
    from app.worker import DWorker
    assert Path(importlib.import_module("app").__file__).resolve().is_relative_to(args.d_root.resolve())
    store = create_store(url, data_root=raw_root)
    checks = []
    samples = []
    known_secrets = [settings["CSRF_SECRET"]]
    def check(name, okay, **details):
        checks.append({"name": name, "status": "passed" if okay else "failed", **details})
        return okay
    def body(response):
        value = response.json()
        return value.get("data", value)
    def record(name, response):
        def scrub(value):
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items() if k.lower() not in {"csrf_token", "password", "password_hash", "lease_token"}}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            return value
        samples.append({"name": name, "status_code": response.status_code, "body": scrub(response.json())})
    try:
        seed_catalog(store)
        imported = import_real_dataset(store, root / "real_data/2026-10-12-v2/manifest.json")
        check("existing_real_C_data_imported", imported["counts"]["imported"] == 83, counts=imported["counts"])
        spec = importlib.util.spec_from_file_location("csv_configuration", root / "examples/configure_registered_csv_rehearsal.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        configured = module.configure(store, root / "acceptance/C-DE-receiving-followup-2026-10-17/registered-csv-input")
        credentials = {}
        with store.write() as uow:
            for role in ("admin", "viewer"):
                password = secrets.token_urlsafe(24)
                credentials[role] = {"username": "synthetic-mainline-" + role, "password": password}
                known_secrets.append(password)
                uow.auth.create_user(UserCreate(credentials[role]["username"], "SYNTHETIC mainline test " + role,
                    PasswordService.hash_password(password), role))
        job_request = {"source_ids": [configured["source_id"]], "dry_run": False, "reason": "C existing real CSV acceptance"}
        with TestClient(app, base_url="http://testserver", raise_server_exceptions=False) as client:
            response = client.post("/api/v1/jobs/ingestion", json=job_request)
            check("29_anonymous_rejected", response.status_code == 401); record("29_anonymous", response)
            response = client.post("/api/v1/auth/login", json=credentials["admin"])
            assert response.status_code == 200, "Admin login failed"
            token = body(response)["csrf_token"]; known_secrets.append(token)
            known_secrets.extend(client.cookies.values())
            headers = {"X-CSRF-Token": token}
            check("admin_login_real_C_auth", True)
            response = client.post("/api/v1/jobs/ingestion", json=job_request)
            check("29_missing_CSRF_rejected", response.status_code == 403); record("29_missing_CSRF", response)
            response = client.post("/api/v1/jobs/ingestion", json=job_request, headers=headers)
            record("29_queued", response)
            assert response.status_code == 202, "29 did not enqueue"
            job_id = body(response)["job_id"]
            check("29_enqueues_C_job", body(response)["status"] == "queued", job_id=job_id)
            check("D_worker_does_not_steal_C_ingestion", DWorker(get_c_store()).run_once() is None)
            with store.read() as uow:
                check("C_ingestion_still_queued", uow.jobs.get_job(job_id)["status"] == "queued")
            result = run_ingestion_once(store, job_id=job_id)
            check("C_worker_real_CSV_success", result["status"] == "succeeded" and result["counts"]["skipped"] == 1,
                result=result)
            response = client.get("/api/v1/jobs/" + job_id)
            record("30_completed", response)
            check("30_reads_completed_C_job", response.status_code == 200 and body(response)["status"] == "succeeded"
                and body(response)["processed_count"] == 1 and body(response)["imported_count"] == 0)
            response = client.post("/api/v1/jobs/ingestion", json={**job_request, "reason": "same source, another reason"}, headers=headers)
            record("29_repeat_after_completion", response)
            check("29_repeat_returns_same_completed_job", response.status_code == 202 and body(response)["job_id"] == job_id
                and body(response)["status"] == "succeeded")
            check("C_reconsume_completed_job_returns_none", run_ingestion_once(store, job_id=job_id) is None)
            query = {"message": "合肥新桥机场2025年1月客流是多少", "context": {
                "metric": "airport_throughput", "entity_ids": ["28380b0b-e209-5ced-8d09-58854248f26e"],
                "period_start": "2025-01-01", "period_end": "2025-01-31", "grain": "month"}}
            response = client.post("/api/v1/assistant/chat", json=query, headers=headers)
            record("19_real_historical_value", response)
            answer = body(response)
            check("19_C_D_E_real_metric_answer", response.status_code == 200 and answer.get("status") == "answered"
                and answer.get("metric_result", {}).get("value") == 1111644
                and bool(answer.get("metric_result", {}).get("source_ids")) and bool(answer.get("citations")))
            response = client.post("/api/v1/jobs/ingestion", json={**job_request, "dry_run": True}, headers=headers)
            assert response.status_code == 202
            dry_id = body(response)["job_id"]
            dry_result = run_ingestion_once(store, job_id=dry_id)
            with store.read() as uow:
                check("dry_run_preserves_observations", dry_result["dry_run"] and uow.jobs.get_job(dry_id)["imported_count"] == 0)
            with TestClient(app, base_url="http://testserver", raise_server_exceptions=False) as viewer:
                response = viewer.post("/api/v1/auth/login", json=credentials["viewer"])
                assert response.status_code == 200
                view_token = body(response)["csrf_token"]; known_secrets.append(view_token)
                known_secrets.extend(viewer.cookies.values())
                response = viewer.post("/api/v1/jobs/ingestion", json=job_request, headers={"X-CSRF-Token": view_token})
                check("29_viewer_rejected", response.status_code == 403); record("29_viewer", response)
                response = viewer.get("/api/v1/jobs/" + job_id)
                check("30_viewer_rejected", response.status_code == 403)
            response = client.post("/api/v1/auth/logout", headers=headers)
            check("logout_invalidates_C_session", response.status_code == 200 and client.get("/api/v1/jobs/" + job_id).status_code == 401)
        with store.read() as uow:
            from data_foundation.db.models import Observation
            check("no_new_or_revised_observations", uow.session.query(Observation).count() == 83)
        actual = set(inspect(store.engine).get_table_names())
        ownership = {"C_tables": len(actual & set(Base.metadata.tables)), "other_tables": sorted(actual - set(Base.metadata.tables) - {"alembic_version"}),
            "alembic_version_present": "alembic_version" in actual, "database_total": len(actual)}
        report = {"status": "passed" if all(row["status"] == "passed" for row in checks) else "failed",
            "scope": ("C_run_D_candidate_ASGI_HTTP_and_C_MySQL_not_D_host_receiving" if args.candidate_patch
                      else "C_run_unmodified_D_ASGI_HTTP_and_C_MySQL_not_D_host_receiving"), "D_commit": args.d_commit,
            "packages": packages, "checks": checks, "http_samples": samples, "table_ownership": ownership,
            "synthetic_login_accounts": True, "real_existing_C_observations": True,
            "D_upstream_repository_modified": False, "D_snapshot_modified": args.candidate_patch is not None,
            "E_source_modified": False, "C_wheel_changed": False,
            "business_DB_touched": False, "live_network_API": False, "real_video_inference": False,
            "crawler_touched": False}
        report["mysql_network_listener_disabled"] = socket_only
        report["D_snapshot_matches_base_commit"] = args.candidate_patch is None
        report["candidate_patch_sha256"] = hashlib.sha256(args.candidate_patch.read_bytes()).hexdigest() if args.candidate_patch else None
        report["candidate_patch_only_in_disposable_copy"] = args.candidate_patch is not None
        serialized = json.dumps(report, ensure_ascii=False, indent=2, default=encode) + "\n"
        assert all(secret not in serialized for secret in known_secrets)
        assert url not in serialized
        (args.output / "report.json").write_text(serialized, encoding="utf-8")
        print(json.dumps({"status": report["status"], "checks": len(checks), "passed": sum(row["status"] == "passed" for row in checks),
            "table_ownership": ownership}, ensure_ascii=False))
    finally:
        store.dispose()


if __name__ == "__main__":
    main()
