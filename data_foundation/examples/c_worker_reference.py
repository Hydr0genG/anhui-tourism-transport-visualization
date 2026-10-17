"""C-owned reference calls for D's missing worker branches.

D still owns claiming, continuous heartbeat, HTTP and failure handling. These
helpers do not claim jobs or catch failures. Success completes the C job inside
the C publication transaction and returns D's existing completion sentinel.
No raw video/ROI/count results are stored in a D private table here.
"""
from datetime import datetime, timezone

from data_foundation import Conflict


PROVENANCE_KEYS = {"count_model", "weights_version", "weights_sha256", "model_source_revision",
                   "confidence_threshold", "inference_engine", "device"}


def heartbeat(store, lease):
    with store.write() as uow:
        uow.jobs.heartbeat(lease, datetime.now(timezone.utc), 600)


def run_video_analysis(store, *, analysis_id, lease, counter, calculate=None):
    """Use registered/authorized C input; E performs calculation outside locks.

The optional calculate callback is for isolated contract tests. A production
caller leaves it unset and uses E's actual recorded-video decoder/counter.
D must renew continuously during long inference, beyond the boundary calls.
"""
    from intelligent_analysis.contracts import CountsResponse
    from intelligent_analysis.worker.video_worker import process_video_job
    reference = store.get_visible_input(analysis_id, lease)
    with store.read() as uow:
        registered = uow.visible.get_analysis(analysis_id)
    if any(counter.provenance.get(key) != registered["provenance"].get(key) for key in PROVENANCE_KEYS):
        raise Conflict("本次counter与C冻结模型溯源不一致")
    counter.validate_config(reference["config"])
    with store.write() as uow:
        uow.visible.begin_analysis(analysis_id, lease)
    heartbeat(store, lease)
    raw = (calculate or process_video_job)(reference, counter=counter)
    parsed = CountsResponse.model_validate(raw)
    if (str(parsed.analysis_id) != analysis_id or str(parsed.video_id) != reference["video_id"]
            or parsed.status != "ready" or parsed.analysis.model_dump(mode="json", exclude_none=True) != reference["config"]
            or parsed.recorded_start_at != datetime.fromisoformat(reference["recorded_start_at"])
            or parsed.recorded_end_at != datetime.fromisoformat(reference["recorded_end_at"])):
        raise Conflict("E结果与本次C分析/录像/配置身份不一致")
    result = parsed.model_dump(mode="json")
    heartbeat(store, lease)
    for index in range(0, len(result["points"]), 1000):
        with store.write() as uow:
            uow.visible.write_points(analysis_id, result["points"][index:index+1000], lease)
    with store.write() as uow:
        uow.visible.complete_analysis(analysis_id, len(result["points"]), lease,
            manual_validation=result.get("manual_validation"))
    return {"analysis_id": analysis_id, "point_count": len(result["points"]), "_job_already_completed": True}


def run_visible_forecast(store, *, analysis_id, cutoff_at, horizon_minutes, lease):
    """C prepares immutable inputs, E predicts, C publishes and completes once."""
    from intelligent_analysis.service import forecast_from_count_history
    with store.write() as uow:
        uow.jobs.ensure_lease(lease)
    with store.read() as uow:
        prepared = uow.visible.forecast_input(analysis_id, cutoff_at)
    history = [(datetime.fromisoformat(point["at"]), point["value"]) for point in prepared["history"]]
    heartbeat(store, lease)
    result = forecast_from_count_history(history, cutoff_at, horizon_minutes, 5)
    heartbeat(store, lease)
    with store.write() as uow:
        saved = uow.visible.publish_forecast(analysis_id=analysis_id, cutoff_at=cutoff_at,
            horizon_minutes=horizon_minutes, input_sha256=prepared["input_sha256"], result=result, lease=lease)
    return {"forecast_id": saved["id"], "status": result["status"], "_job_already_completed": True}


def run_knowledge_invalidate(store, *, document_id, reason, operator_id, lease):
    """Admin identity/payload/lease are revalidated by C; do not complete twice."""
    with store.write() as uow:
        result = uow.knowledge.invalidate_document(document_id=document_id, reason=reason,
            operator_id=operator_id, lease=lease)
    return {**result, "_job_already_completed": True}
