"""C/E official-wheel acceptance on ONE historical notice, not D/HTTP acceptance.

Requires an exclusive EMPTY MySQL schema ending in _test and a NEW run directory.
Never drops tables, deletes assets, modifies D/E source, or opens a business DB.
Run with an isolated Python >=3.13 containing both pinned wheels (no PYTHONPATH).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import subprocess
import sys
from unittest.mock import patch
from zipfile import ZipFile


DOC_ID = "0c8e2020-2e0a-5842-bce9-d9193cf81095"
DOC_VERSION = "sha256:29aa803a0fa3e009feed5ce3aa07330e"
E_COMMIT = "ffc0807b798959563f31256172d20438ee187f98"
D_COMMIT = "0cefaf3cf50b4829928c868ce94e84753827fb40"
PINS = {
    "c_wheel": "4cf48f5c8c4303e2cb73a4169d62dd6111dba03df266256eced48ec565590fbe",
    "c_zip": "7fcbbc0031a13520b5d02aa9ebc8f9066d7b5a237609b0b2c6b0a87a96e54ad5",
    "e_wheel": "5950917b15ba9de3dc474720508bb2bbfbf194e8144b5ed83c2e0cd17b6d9c97",
    "e_zip": "1c9e77bf48de3972e88fdf58ff8d2090cbd78c18f655e33babe8b6115bab73e2",
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def utc_now():
    return datetime.now(timezone.utc)


def guard_url(url):
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if parsed.drivername != "mysql+pymysql" or not (parsed.database or "").endswith("_test"):
        raise ValueError("Only an exclusive empty mysql+pymysql schema ending in _test is allowed")
    return parsed


def safe_member(name):
    value = PurePosixPath(name)
    if (not name or "\0" in name or value.is_absolute() or value.as_posix() != name or "\\" in name
            or any(p in {".", ".."} or ":" in p for p in value.parts)):
        raise ValueError("Unsafe archive member")
    return value


def check_pins(paths):
    for label, expected in PINS.items():
        require(digest(Path(paths[label]).read_bytes()) == expected, label + " SHA256 mismatch")


def installed_package(wheel, module_name, distribution, expected_version):
    require(importlib.metadata.version(distribution) == expected_version, "Installed version mismatch")
    module = importlib.import_module(module_name)
    root = Path(module.__file__).resolve().parent
    site = Path(sys.prefix).resolve()
    require(root.is_relative_to(site) and "site-packages" in root.parts,
            "Package must be installed in this isolated environment, not imported from source")
    count = 0
    with ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith(module_name + "/") and name.endswith((".py", ".json")):
                relative = safe_member(name).relative_to(module_name)
                require((root / relative).read_bytes() == archive.read(name), "Installed module bytes differ")
                count += 1
    require(count > 0, "Wheel contains no package files")
    return {"version": expected_version, "origin": "isolated_site_packages",
            "files_matching_wheel": count, "all_checked_files_match": True}


def extract_inputs(c_zip, e_zip, run_root):
    """Extract only pinned external migrations, one notice, and upstream evidence."""
    bundle = run_root / "c-inputs"
    prefix = "data_foundation/"
    with ZipFile(c_zip) as archive:
        require(archive.testzip() is None, "C ZIP CRC failed")
        manifest = json.loads(archive.read(prefix + "real_data/2026-10-12-v2/manifest.json"))
        doc = next(d for d in manifest["documents"] if d["id"] == DOC_ID)
        asset = next(a for a in manifest["assets"] if a["key"] == doc["asset_key"])
        relative = safe_member(asset["path"])
        content = archive.read(prefix + "real_data/2026-10-12-v2/" + str(relative))
        require(doc["document_version"] == DOC_VERSION, "Unexpected C source version")
        require(digest(content) == asset["sha256"] == asset["text_sha256"], "Notice digest mismatch")
        require(len(content) == asset["byte_size"], "Notice size mismatch")
        source_text = bundle / "notice.txt"
        source_text.parent.mkdir(parents=True)
        source_text.write_bytes(content)
        for name in archive.namelist():
            if name == prefix + "alembic.ini" or name.startswith(prefix + "alembic/") and name.endswith(".py"):
                target = bundle / safe_member(name).relative_to("data_foundation")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    upstream = run_root / "upstream-e051"
    with ZipFile(e_zip) as archive:
        require(archive.testzip() is None, "E ZIP CRC failed")
        for name in archive.namelist():
            if name.startswith(("docs/acceptance/", "docs/fixtures/C020a2-historical-notice/")):
                target = upstream / safe_member(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    fixture = upstream / "docs/fixtures/C020a2-historical-notice"
    sample = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    raw = (fixture / "index.json").read_bytes()
    require(sample["document_id"] == DOC_ID and sample["document_version"] == DOC_VERSION,
            "E sample identity mismatch")
    require(sample["sha256"] == digest(raw) and sample["byte_size"] == len(raw), "E sample integrity failed")
    require(sample["parser_version"] == "text-parser-0.5.0"
            and sample["embedding_model_version"] == "keyword-tfidf-0.5.0"
            and sample["schema_version"] == 1 and sample["chunk_count"] == 3, "Unexpected E sample format")
    return bundle, manifest, doc, asset, source_text, sample, raw


def migrate(url, bundle):
    from alembic import command
    from alembic.config import Config
    config = Config(str(bundle / "alembic.ini"))
    # Extracted directory has no C Python package, so prepend_sys_path cannot shadow wheel.
    previous = {k: os.environ.get(k) for k in ("DATABASE_URL", "DATA_FOUNDATION_MIGRATION_STAGE")}
    os.environ["DATABASE_URL"] = url
    os.environ.pop("DATA_FOUNDATION_MIGRATION_STAGE", None)
    try:
        command.upgrade(config, "head")
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def configure_answers(store, assets):
    from intelligent_analysis import ServiceBindings, configure_services
    from intelligent_analysis.knowledge.artifacts import load_verified_knowledge_snapshot
    configure_services(ServiceBindings(knowledge_snapshot_loader=lambda:
        load_verified_knowledge_snapshot(store, [DOC_ID], assets)))


def probe(url, run_root):
    from data_foundation import create_store
    from intelligent_analysis import answer_message
    store = create_store(url, data_root=run_root / "assets")
    try:
        configure_answers(store, run_root / "assets")
        answer = answer_message("2025年春节黄山索道运营时间")
        require(any(c["id"] == DOC_ID for c in answer["citations"]), "Fresh process lost C citation")
        return {"pid": os.getpid(), "answer": answer}
    finally:
        store.dispose()


def verify(url, run_root, paths):
    from sqlalchemy import create_engine, inspect, select, text
    from data_foundation import AssetCreate, Conflict, EntityCreate, JobCreate, JobLease, LeaseLost, SourceCreate, create_store
    from data_foundation.db.models import JobAttempt, KnowledgeIndex
    from data_foundation.repositories import JobRepository, KnowledgeRepository
    from intelligent_analysis import ServiceBindings, answer_message, configure_services
    from intelligent_analysis.integration import DependencyUnavailable
    from intelligent_analysis.knowledge.artifacts import load_verified_knowledge_snapshot
    from intelligent_analysis.worker import process_knowledge_job

    guard_url(url)
    check_pins(paths)
    packages = {
        "C": installed_package(paths["c_wheel"], "data_foundation", "anhui-data-foundation", "0.2.0a2"),
        "E": installed_package(paths["e_wheel"], "intelligent_analysis", "shumei-intelligent-analysis", "0.5.1"),
    }
    require(not run_root.exists(), "Use a NEW run directory; existing files are never overwritten")
    engine = create_engine(url)
    try:
        require(not inspect(engine).get_table_names() and not inspect(engine).get_view_names(), "Test schema is not empty")
    finally:
        engine.dispose()
    run_root.mkdir(parents=True)
    bundle, manifest, doc, asset, source_text, sample, sample_bytes = extract_inputs(
        paths["c_zip"], paths["e_zip"], run_root)
    migrate(url, bundle)
    store = create_store(url, data_root=run_root / "assets")
    checks, trace, versions = [], [], []
    report = {"scope": "C_E_official_wheels_one_real_notice_isolated_mysql",
        "formal_DE_knowledge_chain": "pending_D_adaptation_and_HTTP_receipt",
        "D_runtime_executed": False, "HTTP_executed": False, "Windows_executed_here": False,
        "C_release_commit": "06d192f068817aa9ffd24ad988e92d9737592aff",
        "E_release_commit": E_COMMIT, "D_review_commit": D_COMMIT,
        "packages": packages, "package_sha256": PINS, "python": platform.python_version(),
        "platform": platform.platform(), "document_id": DOC_ID, "document_version": DOC_VERSION,
        "checks": checks, "transaction_trace": trace, "versions": versions,
        "upstream_E_sample": sample, "verified_at": utc_now().isoformat(),
        "limitations": ["C-side orchestration, not the D Worker or production database",
            "Fresh E reader process is not an API/Worker restart acceptance",
            "No HTTP status assertions; E DependencyUnavailable must still map to D HTTP 503",
            "Official webpage transcription TXT, not original HTML bytes"]}
    opened = 0

    @contextmanager
    def write(label):
        nonlocal opened
        require(opened == 0, "Nested orchestration write transaction")
        tx = len(trace) + 1
        trace.append({"tx": tx, "event": "enter", "label": label})
        opened += 1
        try:
            with store.write() as uow:
                yield uow, tx
        except Exception:
            trace.append({"tx": tx, "event": "rollback", "label": label})
            raise
        else:
            trace.append({"tx": tx, "event": "commit", "label": label})
        finally:
            opened -= 1

    def passed(name, **evidence):
        checks.append({"name": name, "status": "passed", **evidence})

    def prepare(version, mutation=None):
        with store.read() as uow:
            metadata = uow.knowledge.get_document(DOC_ID)
        metadata = json.loads(json.dumps(metadata, default=lambda value: value.isoformat()))
        payload = {"document_id": DOC_ID, "document_version": metadata["document_version"],
                   "version": version, "file_path": str(source_text), "metadata": metadata}
        # Internal path is intentionally NOT stored in the report/trace or public answer.
        with write("enqueue:" + version) as (uow, _):
            job = uow.jobs.enqueue_or_get(JobCreate("knowledge_index", version, payload, "C/E independent acceptance"), utc_now())
        with write("claim:" + version) as (uow, _):
            lease = JobLease(**uow.jobs.claim_job("c-e051-acceptance", utc_now(), 600,
                job_type="knowledge_index", job_id=job["id"])["lease"])
        require(opened == 0, "E build must occur outside the claiming transaction")
        trace.append({"event": "E_build_outside_transaction", "version": version})
        prepared = process_knowledge_job(payload)
        raw = prepared["artifact_bytes"]
        require(digest(raw) == prepared["artifact_sha256"], "E prepared digest mismatch")
        if mutation:
            artifact = json.loads(raw)
            item = artifact["documents"][DOC_ID]
            if mutation == "text":
                item["chunks"][0]["text"] += " [isolated inconsistent text]"
            elif mutation == "locator":
                item["chunks"][0]["chunk_id"] = "isolated-wrong-locator"
            else:
                item[mutation] = "isolated-wrong-version"
            raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode("utf-8")
        require(opened == 0, "persist_file must run outside the database transaction")
        staged = store.persist_file(source_id=doc["source_id"], original_name="index.json", stream=io.BytesIO(raw))
        require((store.data_root / staged["relative_path"]).is_file(), "Artifact must exist before begin/activate")
        trace.append({"event": "native_persist_file_outside_transaction", "version": version,
                      "sha256": staged["sha256"], "byte_size": staged["byte_size"]})
        with write("begin:" + version) as (uow, _):
            index = uow.knowledge.begin_index(document_id=DOC_ID, version=version, job_id=job["id"],
                parser_version=prepared["parser_version"], embedding_model_version=prepared["embedding_model_version"], lease=lease)
        for start in range(0, len(prepared["chunks"]), 2):
            with write("chunks:" + version) as (uow, _):
                uow.knowledge.write_chunks(index["id"], prepared["chunks"][start:start + 2], lease)
        item = {"version": version, "job_id": job["id"], "index_id": index["id"],
                "artifact_sha256": staged["sha256"], "byte_size": staged["byte_size"],
                "chunk_count": prepared["chunk_count"]}
        versions.append(item)
        return prepared, lease, staged, index

    def publish(candidate, wrong_digest=False):
        prepared, lease, staged, index = candidate
        with write("activate_and_complete:" + prepared["version"]) as (uow, tx):
            uow.knowledge.activate_index(document_id=DOC_ID, version=prepared["version"],
                artifact_path=staged["relative_path"], artifact_sha256="0" * 64 if wrong_digest else staged["sha256"],
                expected_artifact_byte_size=staged["byte_size"], expected_chunk_count=prepared["chunk_count"], lease=lease)
            trace.append({"tx": tx, "event": "activate_index", "version": prepared["version"]})
            uow.jobs.complete_job(lease, {"index_id": index["id"]}, utc_now())
            trace.append({"tx": tx, "event": "complete_job", "version": prepared["version"]})

    def state(candidate):
        with store.read() as uow:
            return {"active_version": uow.knowledge.get_document(DOC_ID)["active_index_version"],
                    "job_status": uow.jobs.get_job(candidate[1].job_id)["status"],
                    "index_status": uow.session.get(KnowledgeIndex, candidate[3]["id"]).status}

    def rejects_answer():
        try:
            answer_message("2025年春节黄山运营时间")
        except DependencyUnavailable:
            return
        raise AssertionError("Invalid active artifact was answered or stale memory was reused")

    try:
        with store.read() as uow:
            report["mysql_version"] = uow.session.scalar(text("SELECT VERSION()"))
            report["migration_head"] = uow.session.scalar(text("SELECT version_num FROM alembic_version"))
            report["business_tables"] = len(inspect(uow.session.connection()).get_table_names()) - 1
        require(report["migration_head"] == "0006" and report["business_tables"] == 31, "Schema changed")
        source = next(s for s in manifest["catalog"]["sources"] if s["id"] == doc["source_id"])
        with write("register_source_entities") as (uow, _):
            uow.provenance.register_source(SourceCreate(**source))
            for entity in manifest["catalog"]["entities"]:
                if entity["id"] in doc["entity_ids"]:
                    uow.catalog.create_entity(EntityCreate(**entity))
        staged = store.persist_file(source_id=doc["source_id"], original_name="notice.txt", stream=io.BytesIO(source_text.read_bytes()))
        with write("register_original_document") as (uow, _):
            original = uow.provenance.register_asset(AssetCreate(source_id=doc["source_id"], url=asset["url"],
                title=asset["title"], retrieved_at=datetime.fromisoformat(asset["retrieved_at"]),
                published_at=datetime.fromisoformat(asset["published_at"]), media_type=asset["media_type"],
                usage_note=asset["usage_note"], **staged))
            values = {k: doc[k] for k in ("id", "source_id", "title", "publisher", "source_url", "permission_note", "document_version")}
            values.update(asset_id=original["id"], **{k: datetime.fromisoformat(doc[k]) for k in ("published_at", "valid_from", "valid_until")})
            uow.knowledge.register_document(values, doc["entity_ids"])
        report.update(source_id=doc["source_id"], asset_id=original["id"], original_sha256=staged["sha256"])
        baseline = prepare("ce051-acceptance-1")
        publish(baseline)
        require(state(baseline) == {"active_version": baseline[0]["version"], "job_status": "succeeded", "index_status": "ready"}, "Publication failed")
        configure_answers(store, store.data_root)
        require(len(load_verified_knowledge_snapshot(store, [DOC_ID], store.data_root)["documents"][DOC_ID]["chunks"]) == 3,
                "Verified E load lost chunks")
        passed("real_C_mysql_job_native_persist_batch_chunks_atomic_publication")
        report["answers"] = {
            "historical": answer_message("2025年春节黄山索道运营时间", context={"as_of": "2025-01-30T12:00:00+08:00"}),
            "current": answer_message("现在黄山索道运营时间"),
        }
        for answer in report["answers"].values():
            require(any(c["id"] == DOC_ID and c.get("chunk_id") and c.get("url") == doc["source_url"]
                        and c.get("date") for c in answer["citations"]), "Incomplete real citation")
            serialized = json.dumps(answer, ensure_ascii=False)
            require(str(run_root) not in serialized and "lease_token" not in serialized and "artifact_bytes" not in serialized,
                    "Internal acceptance data leaked in answer")
            require("1月28日—2月4日" in answer["answer"] and "2025年" in answer["answer"],
                    "Answer does not retain the historical notice's applicability period")
        require("历史资料" in report["answers"]["current"]["answer"]
                and "不能作为当前运营事实" in report["answers"]["current"]["answer"], "Current answer lacks historical warning")
        report["C_applicability"] = {k: doc[k] for k in ("valid_from", "valid_until")}
        report["E_citation_has_structured_applicability_fields"] = False
        # E 0.5.1 Citation only has publication date; D must expose the C period
        # through its agreed HTTP response, which this C-side check cannot certify.
        passed("historical_and_current_answers_keep_C_citations_publication_date_and_warning")
        child = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--probe", "--run-root", str(run_root)],
            env={**os.environ, "TEST_DATABASE_URL": url}, capture_output=True, text=True, timeout=60)
        require(child.returncode == 0, "Fresh E reader failed (details excluded to protect connection configuration)")
        restarted = json.loads(child.stdout)
        require(restarted["pid"] != os.getpid(), "Reader did not restart")
        report["fresh_reader"] = restarted
        passed("new_E_process_reopens_mysql_and_verified_citation", parent_pid=os.getpid(), child_pid=restarted["pid"])
        with store.read() as uow:
            ref = uow.knowledge.get_index_artifact_ref(DOC_ID)
        artifact_path = store.data_root / ref["relative_path"]
        raw = artifact_path.read_bytes()
        try:
            artifact_path.write_bytes(b"isolated-corruption")
            rejects_answer()
            passed("active_artifact_corruption_rejected_without_old_memory")
        finally:
            artifact_path.write_bytes(raw)
        hidden = artifact_path.with_name("acceptance-missing.json")
        require(not hidden.exists(), "Unexpected fault injection file")
        try:
            artifact_path.rename(hidden)
            rejects_answer()
            passed("missing_active_artifact_rejected_without_old_memory")
        finally:
            if hidden.exists():
                hidden.rename(artifact_path)
        candidate = prepare("ce051-failure-and-retry")
        before = state(candidate)
        try:
            publish(candidate, wrong_digest=True)
        except Conflict:
            require(state(candidate) == before, "Bad digest changed committed state")
            passed("wrong_new_digest_preserves_old_active_and_uncompleted_job", state=before)
        else:
            raise AssertionError("Wrong digest accepted")
        for repo, method in ((KnowledgeRepository, "activate_index"), (JobRepository, "complete_job")):
            original_method = getattr(repo, method)

            def fail_after_write(self, *args, **kwargs):
                original_method(self, *args, **kwargs)
                raise RuntimeError("isolated injected failure after " + method)

            try:
                with patch.object(repo, method, fail_after_write):
                    publish(candidate)
            except RuntimeError as exc:
                require(str(exc) == "isolated injected failure after " + method, "Unexpected failure")
                require(state(candidate) == before, "Failed final transaction left partial publication")
                passed(method + "_exception_rolls_back_entire_final_transaction", state=before)
            else:
                raise AssertionError("Fault injection did not execute")
        old_lease = candidate[1]
        with write("heartbeat") as (uow, _):
            uow.jobs.heartbeat(old_lease, utc_now(), 600)
        passed("heartbeat_in_independent_short_transaction")
        with write("isolated_expiry_injection") as (uow, _):
            attempt = uow.session.scalar(select(JobAttempt).where(JobAttempt.job_id == old_lease.job_id,
                JobAttempt.attempt_no == old_lease.attempt_no))
            attempt.lease_expires_at = (utc_now() - timedelta(seconds=1)).replace(tzinfo=None)
        with write("recover_expired") as (uow, _):
            require(uow.jobs.recover_expired(utc_now()) == {"requeued": 1, "failed": 0}, "Recovery did not requeue")
        with write("reclaim") as (uow, _):
            new_lease = JobLease(**uow.jobs.claim_job("c-e051-new-attempt", utc_now(), 600, job_id=old_lease.job_id)["lease"])
        require(new_lease.attempt_no == old_lease.attempt_no + 1, "Attempt was not advanced")
        try:
            publish(candidate)
        except LeaseLost:
            passed("old_lease_cannot_publish_after_real_C_recovery", old_attempt=old_lease.attempt_no, new_attempt=new_lease.attempt_no)
        else:
            raise AssertionError("Stale lease published")
        with write("retry_begin_same_index") as (uow, _):
            resumed = uow.knowledge.begin_index(document_id=DOC_ID, version=candidate[0]["version"], job_id=new_lease.job_id,
                parser_version=candidate[0]["parser_version"], embedding_model_version=candidate[0]["embedding_model_version"], lease=new_lease)
            require(resumed["id"] == candidate[3]["id"], "Retry created a duplicate index")
        with write("retry_same_chunks") as (uow, _):
            replay = uow.knowledge.write_chunks(resumed["id"], candidate[0]["chunks"], new_lease)
        require(replay == {"inserted": 0, "skipped": 3}, "Retry duplicated committed chunks")
        candidate = (candidate[0], new_lease, candidate[2], candidate[3])
        publish(candidate)
        require(state(candidate)["job_status"] == "succeeded", "New lease could not publish")
        passed("new_lease_reuses_index_skips_same_chunks_and_publishes", replay=replay)
        for mutation in ("text", "locator", "document_version", "index_version"):
            broken = prepare("ce051-mismatch-" + mutation, mutation)
            publish(broken)
            with store.read() as uow:
                # Correct bytes/SHA pass C; only E's semantic checks can reject this artifact.
                require(uow.knowledge.get_index_artifact_ref(DOC_ID)["sha256"] == broken[2]["sha256"], "C integrity reference failed")
            try:
                rejects_answer()
                passed("correct_SHA_but_" + mutation + "_mismatch_rejected_by_E")
            finally:
                repaired = prepare("ce051-restored-after-" + mutation)
                publish(repaired)
                require(answer_message("2025年春节黄山")["citations"], "Clean replacement did not load")
        try:
            configure_services(ServiceBindings(knowledge_index_path="unused.json", knowledge_snapshot_loader=lambda: {}))
        except ValueError:
            passed("snapshot_loader_and_index_path_are_mutually_exclusive")
        else:
            raise AssertionError("Conflicting knowledge bindings accepted")
        # Validate the actual upstream E fixture in the same MySQL store as a final version.
        fixture_candidate = prepare(sample["index_version"])
        fixture_staged = store.persist_file(source_id=doc["source_id"], original_name="index.json", stream=io.BytesIO(sample_bytes))
        fixture_candidate = (fixture_candidate[0], fixture_candidate[1], fixture_staged, fixture_candidate[3])
        versions[-1].update(artifact_sha256=fixture_staged["sha256"], byte_size=fixture_staged["byte_size"])
        publish(fixture_candidate)
        snapshot = load_verified_knowledge_snapshot(store, [DOC_ID], store.data_root)
        require(snapshot["documents"][DOC_ID]["index_version"] == sample["index_version"], "Actual E fixture failed verification")
        passed("actual_E_release_index_json_and_manifest_match_MySQL_chunks")
        report["final_state"] = state(fixture_candidate)
        report["status"] = "passed"
        report["check_count"] = len(checks)
        return report
    finally:
        configure_services(ServiceBindings())
        store.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    for label in PINS:
        parser.add_argument("--" + label.replace("_", "-"), type=Path)
    args = parser.parse_args()
    try:
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            raise ValueError("Set TEST_DATABASE_URL for an exclusive empty MySQL schema")
        guard_url(url)
        if args.probe:
            print(json.dumps(probe(url, args.run_root.resolve()), ensure_ascii=False))
            return 0
        require(args.report is not None and not args.report.exists(), "Use a NEW report file")
        paths = {label: getattr(args, label) for label in PINS}
        require(all(paths.values()), "Provide all four pinned wheel/ZIP paths")
        report = verify(url, args.run_root.resolve(), paths)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "checks": report["check_count"], "scope": report["scope"]}))
        return 0
    except Exception as exc:
        # Do not print URLs, credentials, internal paths or raw third-party exceptions.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
