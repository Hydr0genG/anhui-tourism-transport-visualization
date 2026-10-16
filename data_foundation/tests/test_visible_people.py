"""Synthetic storage acceptance only: not real video inference/MAE evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from statistics import median
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from data_foundation import AssetCreate, Conflict, InvalidArgument, InvalidPermission, JobCreate, JobLease, LeaseLost, NotFound, UserCreate
from data_foundation.db.models import PermissionRecord, VisibleAnalysis, VisibleCountPoint, VisibleForecast
from data_foundation.repositories import JobRepository
from conftest import CAMERA_ID, SCENIC_ID, SOURCE_ID


CONFIG = {"count_model": "p2pnet", "weights_version": "synthetic-storage-fixture",
          "roi": {"points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}]},
          "sample_interval_seconds": 10, "aggregation_minutes": 5, "confidence_threshold": 0.5,
          "inference_engine": "pytorch", "device": "CPU"}
PROVENANCE = {k:v for k,v in CONFIG.items() if k in {"count_model","weights_version","confidence_threshold","inference_engine","device"}}
PROVENANCE.update(weights_sha256="a"*64, model_source_revision="SYNTHETIC_NOT_A_REAL_MODEL")


def setup_visible(store, tmp_path, duration=300, start=None):
    now = datetime.now(timezone.utc)
    start = start or datetime(2025, 1, 1, 0, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
    store.data_root = tmp_path / "inputs"
    raw = b"SYNTHETIC STORAGE FIXTURE, NOT A DECODABLE OR AUTHORIZED REAL RECORDING"
    original = store.persist_file(source_id=SOURCE_ID, original_name="fixture.bin", stream=BytesIO(raw))
    with store.write() as uow:
        asset = uow.provenance.register_asset(AssetCreate(SOURCE_ID, "https://example.invalid/storage-fixture", "synthetic",
            now, original["relative_path"], original["sha256"], "application/octet-stream", len(raw), "SYNTHETIC TEST ONLY"))
        permission = uow.video.register_permission({"source_id": SOURCE_ID, "grantor": "SYNTHETIC TEST ONLY",
            "basis": "SYNTHETIC STORAGE TEST, NOT A REAL AUTHORIZATION", "evidence_sha256": "b"*64,
            "evidence_path": "synthetic-no-real-authorization", "allowed_uses": ["analysis"], "playback_allowed": False,
            "valid_from": now-timedelta(days=1), "valid_until": now+timedelta(days=1), "status": "approved"})
        video = uow.video.register_video({"source_id": SOURCE_ID, "asset_id": asset["id"], "scenic_id": SCENIC_ID,
            "camera_id": CAMERA_ID, "permission_record_id": permission["id"], "recorded_start_at": start,
            "recorded_end_at": start+timedelta(seconds=duration)}, now)
        identity = str(uuid4())
        job = uow.jobs.enqueue_or_get(JobCreate("video_analysis", str(uuid4()), {"video_id": video["id"], "analysis_id": identity}, "synthetic storage acceptance"), now)
        analysis = uow.visible.create_analysis(video_id=video["id"], job_id=job["id"], config=CONFIG, provenance=PROVENANCE, analysis_id=identity)
        assert uow.visible.create_analysis(video_id=video["id"], job_id=job["id"], config=CONFIG, provenance=PROVENANCE, analysis_id=identity)["id"] == identity
    with store.write() as uow:
        lease = JobLease(**uow.jobs.claim_job("synthetic-worker", now, 600, job_id=job["id"])["lease"])
    return {"store": store, "now": now, "start": start, "video": video, "analysis": analysis, "job": job,
            "lease": lease, "permission_id": permission["id"], "duration": duration}


@pytest.fixture
def visible(store, tmp_path):
    return setup_visible(store, tmp_path)


def points(f, frames=30, windows=1):
    result = [{"metric": "camera_visible_people", "unit": "人", "grain": "frame", "value": float(i),
        "recorded_at": (f["start"]+timedelta(seconds=i*10)).isoformat(), "sample_count": 1,
        "quality_status": "good", "source_ids": [SOURCE_ID]} for i in range(frames)]
    for w in range(windows):
        values = [p["value"] for p in result if p["grain"] == "frame" and
                  f["start"]+timedelta(seconds=300*w) <= datetime.fromisoformat(p["recorded_at"]) < f["start"]+timedelta(seconds=300*(w+1))]
        result.append({"metric": "camera_visible_people", "unit": "人", "grain": "5min",
            "period_start": (f["start"]+timedelta(seconds=300*w)).isoformat(),
            "period_end": (f["start"]+timedelta(seconds=300*(w+1))).isoformat(),
            "value": float(median(values)) if values else None, "sample_count": len(values),
            "quality_status": "unavailable" if not values else "good" if len(values) == 30 else "degraded",
            "aggregation_rule": "median", "source_ids": [SOURCE_ID]})
    return result


def write(f, data=None):
    with f["store"].write() as uow:
        uow.visible.begin_analysis(f["analysis"]["id"], f["lease"])
        return uow.visible.write_points(f["analysis"]["id"], data if data is not None else points(f), f["lease"])


def complete(f, data=None):
    data = data if data is not None else points(f)
    write(f, data)
    with f["store"].write() as uow:
        return uow.visible.complete_analysis(f["analysis"]["id"], len(data), f["lease"])


def test_native_fractional_median_atomic_job_completion_and_e_shape(visible):
    f = visible
    with f["store"].read() as uow:
        assert uow.visible.get_counts(f["video"]["id"], f["analysis"]["id"])["points"] == []
    assert write(f) == {"inserted": 31, "skipped": 0}
    with f["store"].write() as uow:
        assert uow.visible.write_points(f["analysis"]["id"], points(f), f["lease"]) == {"inserted": 0, "skipped": 31}
        uow.visible.complete_analysis(f["analysis"]["id"], 31, f["lease"])
    with f["store"].read() as uow:
        result = uow.visible.get_counts(f["video"]["id"], f["analysis"]["id"])
        assert result["status"] == "ready" and result["points"][0]["value"] == 14.5
        assert result["manual_validation"] is None and "provenance" not in result
        assert uow.jobs.get_job(f["job"]["id"])["status"] == "succeeded"
        assert result["analysis"]["count_model"] == "p2pnet"
    with f["store"].write() as uow:
        with pytest.raises(LeaseLost):
            uow.visible.write_points(f["analysis"]["id"], [], f["lease"])


@pytest.mark.parametrize("change", [{"value": -1}, {"value": float("nan")}, {"value": float("inf")},
    {"value": True}, {"value": None}, {"sample_count": True}, {"source_ids": []}, {"source_ids": [str(uuid4())]},
    {"entries": 2}, {"unit": "人次"}, {"recorded_at": "2025-01-01T00:00:00"}, {"grain": "day"}])
def test_invalid_points_are_atomic_and_never_become_ready(visible, change):
    f = visible
    data = points(f)
    data[1] = {**data[1], **change}
    with pytest.raises(InvalidArgument):
        write(f, data)
    with f["store"].read() as uow:
        assert uow.session.scalar(select(func.count()).select_from(VisibleCountPoint)) == 0


def test_mid_batch_conflict_rolls_back_even_if_caller_catches(visible):
    f = visible
    write(f, points(f)[:1])
    with f["store"].write() as uow:
        with pytest.raises(Conflict):
            uow.visible.write_points(f["analysis"]["id"], [points(f)[1], {**points(f)[0], "value": 999.0}], f["lease"])
    with f["store"].read() as uow:
        assert uow.session.scalar(select(func.count()).select_from(VisibleCountPoint)) == 1


@pytest.mark.parametrize("change", [{"value": 99}, {"sample_count": 1}, {"quality_status": "degraded"}])
def test_window_must_match_committed_frames(visible, change):
    f = visible
    data = points(f)
    data[-1].update(change)
    write(f, data)
    with f["store"].write() as uow:
        with pytest.raises(Conflict):
            uow.visible.complete_analysis(f["analysis"]["id"], 31, f["lease"])
    with f["store"].read() as uow:
        assert uow.visible.get_counts(f["video"]["id"], f["analysis"]["id"])["status"] == "analysis_not_ready"


def test_completion_failure_after_job_write_rolls_back_both_even_if_caught(visible):
    f = visible
    write(f)
    original = JobRepository.complete_job
    def fail(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("INJECTED AFTER JOB COMPLETION")
    with f["store"].write() as uow:
        with patch.object(JobRepository, "complete_job", fail), pytest.raises(RuntimeError):
            uow.visible.complete_analysis(f["analysis"]["id"], 31, f["lease"])
    with f["store"].read() as uow:
        assert uow.visible.get_analysis(f["analysis"]["id"])["status"] == "running"
        assert uow.jobs.get_job(f["job"]["id"])["status"] == "running"
    with f["store"].write() as uow:
        uow.visible.complete_analysis(f["analysis"]["id"], 31, f["lease"])


def test_input_checks_analysis_permission_not_playback_and_io_is_outside_transaction(visible, monkeypatch):
    f = visible
    import data_foundation.store as module
    original = module.verify_artifact
    def verify(*args, **kwargs):
        # No connection has been checked out while hashing a potentially long video.
        assert f["store"].engine.pool.checkedout() == 0
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "verify_artifact", verify)
    ref = f["store"].get_visible_input(f["analysis"]["id"], f["lease"])
    assert Path(ref["file_path"]).is_file() and ref["config"]["count_model"] == "p2pnet"
    with f["store"].read() as uow:
        with pytest.raises(InvalidPermission):
            uow.video.get_playback_ref(f["video"]["id"], f["now"])
    Path(ref["file_path"]).write_bytes(b"bad fixture bytes")
    with pytest.raises(Conflict):
        f["store"].get_visible_input(f["analysis"]["id"], f["lease"])


def test_permission_revoked_during_hash_prevents_input(visible, monkeypatch):
    f = visible
    import data_foundation.store as module
    original = module.verify_artifact
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        with f["store"].write() as uow:
            uow.session.get(PermissionRecord, f["permission_id"]).status = "revoked"
        return result
    monkeypatch.setattr(module, "verify_artifact", revoke)
    with pytest.raises(InvalidPermission):
        f["store"].get_visible_input(f["analysis"]["id"], f["lease"])


def test_real_expiry_recovery_old_attempt_rejected_new_attempt_deduplicates(visible):
    f = visible
    write(f)
    recovery = f["now"] + timedelta(seconds=601)
    with f["store"].write() as uow:
        uow.jobs.recover_expired(recovery)
    with f["store"].write() as uow:
        with pytest.raises(LeaseLost):
            uow.visible.complete_analysis(f["analysis"]["id"], 31, f["lease"], now=recovery)
    with f["store"].write() as uow:
        f["lease"] = JobLease(**uow.jobs.claim_job("replacement", recovery + timedelta(seconds=600), 1200, job_id=f["job"]["id"])["lease"])
        uow.visible.begin_analysis(f["analysis"]["id"], f["lease"], now=recovery + timedelta(seconds=600))
        assert uow.visible.write_points(f["analysis"]["id"], points(f), f["lease"], now=recovery + timedelta(seconds=600)) == {"inserted": 0, "skipped": 31}
        uow.visible.complete_analysis(f["analysis"]["id"], 31, f["lease"], now=recovery + timedelta(seconds=600))


@pytest.mark.parametrize("config_patch", [{"device": "GPU"}, {"inference_engine": "openvino"},
    {"count_model": "dm_count"}, {"aggregation_minutes": 15}, {"count_line": {}},
    {"confidence_threshold": 0.4}, {"sample_interval_seconds": True},
    {"roi": {"points": [{"x": 0, "y": 0}, {"x": 1, "y": 1}, {"x": 0, "y": 1}, {"x": 1, "y": 0}]}}])
def test_legacy_or_unverified_configuration_rejected(config_patch):
    from data_foundation.visible import config_and_provenance
    with pytest.raises(InvalidArgument):
        config_and_provenance({**deepcopy(CONFIG), **config_patch}, deepcopy(PROVENANCE))


@pytest.mark.parametrize("frames", [0, 2])
def test_missing_and_degraded_windows_preserved_not_zero(visible, frames):
    f = visible
    complete(f, points(f, frames=frames))
    with f["store"].read() as uow:
        result = uow.visible.get_counts(f["video"]["id"], f["analysis"]["id"])
        window = next(p for p in result["points"] if p["grain"] == "5min")
        assert window["value"] == (None if frames == 0 else 0.5)
        assert window["sample_count"] == frames


def test_forecast_insufficient_is_stored_with_input_fingerprint_and_job(visible):
    f = visible
    complete(f)
    cutoff = f["start"] + timedelta(minutes=5)
    with f["store"].read() as uow:
        history = uow.visible.forecast_input(f["analysis"]["id"], cutoff)
    result = {"metric": "camera_visible_people", "status": "data_insufficient", "forecast_points": [],
              "training_cutoff": cutoff.isoformat(), "model_version": "synthetic-storage-test",
              "validation_error": None, "reason": "Insufficient continuous history"}
    with f["store"].write() as uow:
        job = uow.jobs.enqueue_or_get(JobCreate("visible_forecast", str(uuid4()), {"analysis_id": f["analysis"]["id"],
            "cutoff_at":cutoff.isoformat(),"horizon_minutes":15}, "synthetic forecast storage"), f["now"])
    with f["store"].write() as uow:
        lease = JobLease(**uow.jobs.claim_job("forecast", f["now"], 600, job_id=job["id"])["lease"])
        forecast = uow.visible.publish_forecast(analysis_id=f["analysis"]["id"], cutoff_at=cutoff,
            horizon_minutes=15, input_sha256=history["input_sha256"], result=result, lease=lease)
    with f["store"].read() as uow:
        row = uow.visible.get_forecast(forecast["id"])
        assert row["result"]["status"] == "data_insufficient" and row["result"]["forecast_points"] == []
        assert row["input_sha256"] == history["input_sha256"] and row["analysis_id"] == f["analysis"]["id"]
        assert uow.jobs.get_job(job["id"])["status"] == "succeeded"


def forecast_job(f, cutoff_at=None, horizon_minutes=15):
    cutoff_at = cutoff_at or f["start"]+timedelta(minutes=5)
    with f["store"].write() as uow:
        job = uow.jobs.enqueue_or_get(JobCreate("visible_forecast", str(uuid4()), {"analysis_id": f["analysis"]["id"],
            "cutoff_at":cutoff_at.isoformat(),"horizon_minutes":horizon_minutes}, "SYNTHETIC FORECAST TEST"), f["now"])
    with f["store"].write() as uow:
        lease = JobLease(**uow.jobs.claim_job("forecast", f["now"], 600, job_id=job["id"])["lease"])
    return job, lease


def test_e060_roundtrip_ready_forecast_null_bounds_and_atomic_failure(store, tmp_path):
    contracts = pytest.importorskip("intelligent_analysis.contracts")
    from intelligent_analysis.short_forecast.baseline import forecast_visible_history
    f = setup_visible(store, tmp_path, duration=17*300)
    data = points(f, frames=17*30, windows=17)
    complete(f, data)
    cutoff = f["start"] + timedelta(minutes=85)
    with store.read() as uow:
        assert contracts.CountsResponse.model_validate(uow.visible.get_counts(f["video"]["id"],f["analysis"]["id"])).status == "ready"
        history = uow.visible.forecast_input(f["analysis"]["id"], cutoff)
    result = forecast_visible_history([(datetime.fromisoformat(p["at"]),p["value"]) for p in history["history"]],cutoff,15)
    assert result["status"] == "ready"
    job, lease = forecast_job(f,cutoff)
    original = JobRepository.complete_job
    def fail(self,*args,**kwargs):
        original(self,*args,**kwargs)
        raise RuntimeError("FORECAST COMPLETE FAILED AFTER WRITE")
    kwargs = dict(analysis_id=f["analysis"]["id"],cutoff_at=cutoff,horizon_minutes=15,
                  input_sha256=history["input_sha256"],result=result,lease=lease)
    with store.write() as uow:
        with patch.object(JobRepository,"complete_job",fail), pytest.raises(RuntimeError):
            uow.visible.publish_forecast(**kwargs)
    with store.read() as uow:
        assert uow.session.scalar(select(func.count()).select_from(VisibleForecast)) == 0
        assert uow.jobs.get_job(job["id"])["status"] == "running"
    with store.write() as uow:
        row = uow.visible.publish_forecast(**kwargs)
    with store.read() as uow:
        response = contracts.ForecastResponse.model_validate(uow.visible.get_forecast(row["id"])["result"])
        assert response.forecast_points[0].lower is None and response.forecast_points[0].upper is None
        assert response.validation_error["15"]["fold_count"] >= 3


@pytest.mark.parametrize("fault", ["input_sha", "ready60", "half_bounds", "future_time", "training_cutoff"])
def test_forecast_bad_binding_does_not_complete_job(visible, fault):
    f = visible
    complete(f)
    cutoff = f["start"] + timedelta(minutes=5)
    with f["store"].read() as uow:
        history = uow.visible.forecast_input(f["analysis"]["id"], cutoff)
    job, lease = forecast_job(f)
    result = {"metric":"camera_visible_people","status":"data_insufficient","forecast_points":[],
              "training_cutoff":cutoff.isoformat(),"model_version":"synthetic","reason":"Not enough history"}
    digest, horizon = history["input_sha256"],15
    if fault == "input_sha": digest = "f"*64
    elif fault == "training_cutoff": result["training_cutoff"] = (cutoff+timedelta(minutes=5)).isoformat()
    else:
        horizon = 60 if fault == "ready60" else 15
        result.update(status="ready", reason=None, forecast_points=[{"target_time":(cutoff+timedelta(minutes=5*i)).isoformat(),
            "value":10.0,"is_forecast":True,"lower":None,"upper":None} for i in range(1,horizon//5+1)])
        if fault == "half_bounds": result["forecast_points"][0]["lower"] = 1
        if fault == "future_time": result["forecast_points"][0]["target_time"] = cutoff.isoformat()
    with f["store"].write() as uow:
        with pytest.raises((Conflict, InvalidArgument)):
            uow.visible.publish_forecast(analysis_id=f["analysis"]["id"],cutoff_at=cutoff,horizon_minutes=horizon,
                                        input_sha256=digest,result=result,lease=lease)
    with f["store"].read() as uow:
        assert uow.session.scalar(select(func.count()).select_from(VisibleForecast)) == 0
        assert uow.jobs.get_job(job["id"])["status"] == "running"


def test_model_or_recording_binding_cannot_be_changed_on_old_analysis(visible):
    f = visible
    complete(f)
    with f["store"].write() as uow:
        row = uow.session.get(VisibleAnalysis,f["analysis"]["id"])
        row.provenance = {**row.provenance,"weights_sha256":"c"*64}
    with f["store"].read() as uow:
        with pytest.raises(Conflict):
            uow.visible.get_counts(f["video"]["id"],f["analysis"]["id"])


def test_lease_expiry_during_input_hash_rejected(visible, monkeypatch):
    f = visible
    import data_foundation.store as module
    original = module.verify_artifact
    def expire(*args,**kwargs):
        result = original(*args,**kwargs)
        with f["store"].write() as uow:
            assert uow.jobs.recover_expired(f["now"]+timedelta(seconds=601))["requeued"] == 1
        return result
    monkeypatch.setattr(module,"verify_artifact",expire)
    with pytest.raises(LeaseLost):
        f["store"].get_visible_input(f["analysis"]["id"],f["lease"])


def test_manual_validation_unmeasured_is_not_zero_mae(visible):
    f = visible
    write(f)
    with f["store"].write() as uow:
        with pytest.raises(InvalidArgument):
            uow.visible.complete_analysis(f["analysis"]["id"],31,f["lease"],manual_validation={"sample_count":0,"mae":0})


def test_failed_job_admin_retry_keeps_points_and_rejects_old_attempt(visible):
    f=visible
    write(f)
    old=f["lease"]
    with f["store"].write() as uow:
        admin=uow.auth.create_user(UserCreate("visible-retry-"+uuid4().hex,"synthetic","NOT_A_PASSWORD","admin"))
        uow.visible.fail_analysis(f["analysis"]["id"],"synthetic controlled failure",old)
    with f["store"].write() as uow:
        uow.jobs.retry_failed(f["job"]["id"],"synthetic controlled retry",admin["id"],f["now"])
        new=JobLease(**uow.jobs.claim_job("retry",f["now"],600,job_id=f["job"]["id"])["lease"])
    assert new.attempt_no==2
    with f["store"].write() as uow:
        with pytest.raises(LeaseLost):
            uow.visible.fail_analysis(f["analysis"]["id"],"stale attempt must not fail replacement",old)
        uow.visible.begin_analysis(f["analysis"]["id"],new)
        assert uow.visible.write_points(f["analysis"]["id"],points(f),new)=={"inserted":0,"skipped":31}
        uow.visible.complete_analysis(f["analysis"]["id"],31,new)
    with f["store"].read() as uow:
        assert uow.visible.get_counts(f["video"]["id"],f["analysis"]["id"])["status"]=="ready"
