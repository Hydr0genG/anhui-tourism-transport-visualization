"""Verify supplemental real-data input without changing the sealed a2 wheel."""
import importlib.util
from pathlib import Path

import pytest

from data_foundation import Conflict, JobCreate
from data_foundation.ingestion import run_ingestion_once
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "acceptance/C-DE-receiving-followup-2026-10-17/registered-csv-input"
spec = importlib.util.spec_from_file_location("configure_csv_rehearsal", ROOT / "examples/configure_registered_csv_rehearsal.py")
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def test_real_csv_rehearsal_preserves_official_evidence_and_is_idempotent(store, tmp_path):
    store.data_root = tmp_path / "test-input"
    first = example.configure(store, BUNDLE)
    second = example.configure(store, BUNDLE)
    assert first["csv_asset_id"] == second["csv_asset_id"]
    assert first["provenance_asset_id"] == second["provenance_asset_id"]
    assert first["csv_asset_id"] != first["provenance_asset_id"]
    manifest, _ = example.validate_bundle(BUNDLE)
    with store.write() as uow:
        evidence = uow.provenance.get_asset(first["provenance_asset_id"])
        assert evidence["sha256"] == manifest["evidence"]["sha256"]
        job = uow.jobs.enqueue_or_get(JobCreate("ingestion", "real-input-rehearsal", {
            "source_ids": [first["source_id"]], "dry_run": False}, "C existing public data rehearsal"), datetime.now(timezone.utc))
    result = run_ingestion_once(store, job_id=job["id"])
    assert result["status"] == "succeeded" and result["counts"]["imported"] == 1
    assert run_ingestion_once(store, job_id=job["id"]) is None
    with store.read() as uow:
        from data_foundation import SeriesQuery
        from datetime import date
        point = uow.observations.query_series(SeriesQuery(manifest["entity_id"], "airport_throughput", "month",
            date(2025, 1, 1), date(2025, 1, 31)))["points"][0]
        assert point["value"] == 1111644
        assert point["provenance"]["asset_id"] == first["provenance_asset_id"]


def test_rehearsal_tampered_csv_is_rejected_before_database_writes(tmp_path):
    import shutil
    target = tmp_path / "bundle"
    shutil.copytree(BUNDLE, target)
    (target / "hefei-airport-2025-01.csv").write_bytes(b"tampered")
    with pytest.raises(Conflict):
        example.validate_bundle(target)
