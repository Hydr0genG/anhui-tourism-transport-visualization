"""SYNTHETIC wiring only: not real recording/model/authorization acceptance."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

np = pytest.importorskip("numpy", reason="E/C worker reference acceptance requires the E runtime")
pytest.importorskip("intelligent_analysis", reason="E/C worker reference acceptance requires the installed E package")

from data_foundation import Conflict, JobCreate, JobLease
from test_document_invalidation import setup_document
from test_visible_people import CONFIG, PROVENANCE, setup_visible


path = Path(__file__).resolve().parents[1] / "examples/c_worker_reference.py"
spec = importlib.util.spec_from_file_location("c_worker_reference", path)
reference = importlib.util.module_from_spec(spec); spec.loader.exec_module(reference)


class SyntheticCounter:
    provenance = PROVENANCE
    def validate_config(self, config):
        from intelligent_analysis.contracts import VideoAnalysisConfig
        assert VideoAnalysisConfig.model_validate(config).weights_version == CONFIG["weights_version"]
    def __call__(self, frame):
        return np.array([[.25, .25, .9], [.75, .75, .9], [.5, .5, .5]])


def calculate(payload, *, counter):
    from intelligent_analysis.worker.video_worker import FrameData, process_video_job
    frames = [FrameData(i, i * 10, np.zeros((16, 16, 3), dtype=np.uint8)) for i in range(30)]
    return process_video_job(payload, counter=counter, frames=frames)


def test_E_native_result_publishes_C_counts_and_completes_once(store, tmp_path):
    f = setup_visible(store, tmp_path)
    result = reference.run_video_analysis(store, analysis_id=f["analysis"]["id"], lease=f["lease"],
        counter=SyntheticCounter(), calculate=calculate)
    assert result["_job_already_completed"] and result["point_count"] == 31
    with store.read() as uow:
        native = uow.visible.get_counts(f["video"]["id"], f["analysis"]["id"])
        assert native["status"] == "ready" and native["manual_validation"] is None
        assert {point["grain"] for point in native["points"]} == {"frame", "5min"}
        assert all(point["value"] == 2 for point in native["points"])
        assert uow.jobs.get_job(f["job"]["id"])["status"] == "succeeded"


def test_forecast_uses_C_input_and_E_insufficient_result_without_fake_curve(store, tmp_path):
    f = setup_visible(store, tmp_path)
    reference.run_video_analysis(store, analysis_id=f["analysis"]["id"], lease=f["lease"], counter=SyntheticCounter(), calculate=calculate)
    cutoff = (f["start"] + timedelta(minutes=5)).isoformat()
    now = datetime.now(timezone.utc)
    with store.write() as uow:
        job = uow.jobs.enqueue_or_get(JobCreate("visible_forecast", str(uuid4()), {
            "analysis_id": f["analysis"]["id"], "cutoff_at": cutoff, "horizon_minutes": 15}, "synthetic wiring only"), now)
        lease = JobLease(**uow.jobs.claim_job("test", now, 600, job_id=job["id"])["lease"])
    result = reference.run_visible_forecast(store, analysis_id=f["analysis"]["id"], cutoff_at=cutoff,
        horizon_minutes=15, lease=lease)
    assert result["status"] == "data_insufficient" and result["_job_already_completed"]
    with store.read() as uow:
        saved = uow.visible.get_forecast(result["forecast_id"])
        assert saved["result"]["forecast_points"] == [] and saved["result"]["reason"]
        assert uow.jobs.get_job(job["id"])["status"] == "succeeded"


def test_invalidation_reference_keeps_audit_and_completes_same_C_job(store, tmp_path):
    doc, operator, job, lease = setup_document(store, tmp_path)
    result = reference.run_knowledge_invalidate(store, document_id=doc["id"], reason="synthetic review",
        operator_id=operator["id"], lease=lease)
    assert result["_job_already_completed"]
    with store.read() as uow:
        assert uow.knowledge.get_index_artifact_ref(doc["id"])["status"] == "index_not_ready"
        assert uow.jobs.get_job(job["id"])["status"] == "succeeded"


def test_wrong_actual_model_provenance_is_rejected_before_publication(store, tmp_path):
    f = setup_visible(store, tmp_path)
    counter = SyntheticCounter(); counter.provenance = {**PROVENANCE, "weights_sha256": "b" * 64}
    with pytest.raises(Conflict):
        reference.run_video_analysis(store, analysis_id=f["analysis"]["id"], lease=f["lease"], counter=counter, calculate=calculate)
    with store.read() as uow:
        assert uow.visible.get_counts(f["video"]["id"], f["analysis"]["id"])["points"] == []


def test_equivalent_recording_timezone_is_the_same_input_identity(store, tmp_path):
    f = setup_visible(store, tmp_path)
    def equivalent(payload, *, counter):
        result = calculate(payload, counter=counter)
        for key in ("recorded_start_at", "recorded_end_at"):
            result[key] = datetime.fromisoformat(result[key]).astimezone(timezone(timedelta(hours=8))).isoformat()
        return result
    result = reference.run_video_analysis(store, analysis_id=f["analysis"]["id"], lease=f["lease"],
        counter=SyntheticCounter(), calculate=equivalent)
    assert result["_job_already_completed"] and result["point_count"] == 31
