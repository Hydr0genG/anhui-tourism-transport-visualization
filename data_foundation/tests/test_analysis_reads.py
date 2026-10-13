from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from data_foundation import AnalysisNotReady, Conflict, CountInput, InvalidArgument, InvalidPermission, NotFound, Page, QueryLimitExceeded
from data_foundation.db.models import ManualAnnotation, PermissionRecord, Video
from data_foundation.seed import seed_catalog
from test_worker_lifecycle import fx


def annotation(f, **patch):
    return {"video_id":f["video"]["id"],"camera_id":f["camera_id"],"sample_key":"test-sample","version":1,
        "window_start":f["now"],"window_end":f["now"]+timedelta(minutes=5),"entries":10,"exits":3,
        "annotator":"synthetic-reviewer","direction_note":"仅测试方向说明","review_status":"verified",**patch}


def ready_counts(store,f,*,positions=(0,1,2,3,4,5),qualities=None):
    with store.write() as uow:
        video = uow.session.get(Video,f["video"]["id"])
        video.recorded_end_at = (f["now"]+timedelta(minutes=30)).replace(tzinfo=None)
        points = [CountInput(f["now"]+timedelta(minutes=5*i),f["now"]+timedelta(minutes=5*(i+1)),i+1,2*(i+1),
            (qualities or {}).get(i,"verified")) for i in positions]
        uow.video.write_counts(f["analysis"]["id"],points,f["video_lease"])
        uow.video.complete_analysis(f["analysis"]["id"],len(points),f["video_lease"])
    return points


def test_annotations_immutable_versions_latest_review_and_pagination(fx):
    store,f = fx
    with store.write() as uow:
        assert uow.video.write_manual_annotations([annotation(f)],now=f["now"]) == {"inserted":1,"skipped":0}
        assert uow.video.write_manual_annotations([annotation(f)],now=f["now"]) == {"inserted":0,"skipped":1}
        assert uow.video.write_manual_annotations([annotation(f,version=2,entries=12,review_status="rejected")],now=f["now"])["inserted"] == 1
        assert uow.video.write_manual_annotations([annotation(f,sample_key="next-sample")],now=f["now"])["inserted"] == 1
    with store.read() as uow:
        assert uow.video.list_manual_annotations(f["video"]["id"])["total"] == 2
        assert uow.video.list_manual_annotations(f["video"]["id"],latest_only=False)["total"] == 3
        verified = uow.video.list_manual_annotations(f["video"]["id"],review_status="verified")
        assert verified["total"] == 1 and verified["items"][0]["sample_key"] == "next-sample"
        assert uow.video.list_manual_annotations(f["video"]["id"],review_status="verified",latest_only=False)["total"] == 2
        first = uow.video.list_manual_annotations(f["video"]["id"],page=Page(1,1))["items"][0]["id"]
        second = uow.video.list_manual_annotations(f["video"]["id"],page=Page(2,1))["items"][0]["id"]
        assert first != second


@pytest.mark.parametrize("patch",[{"entries":-1},{"exits":True},{"version":False},{"version":1.5},{"annotator":" "},{"sample_key":"x"*101},
    {"direction_note":""},{"review_status":"unknown"},{"review_status":[]},{"window_start":None},{"window_end":"invalid"}])
def test_bad_annotation_rejected_without_writes(fx,patch):
    store,f = fx
    with store.write() as uow:
        with pytest.raises(InvalidArgument): uow.video.write_manual_annotations([annotation(f,**patch)],now=f["now"])
        assert uow.video.list_manual_annotations(f["video"]["id"],latest_only=False)["total"] == 0


@pytest.mark.parametrize("patch",[{"camera_id":str(uuid4())},{"window_start":None},{"window_end":None}])
def test_annotation_video_bounds(fx,patch):
    store,f = fx
    if "window_start" in patch:patch={"window_start":f["now"]-timedelta(microseconds=1)}
    if "window_end" in patch:patch={"window_end":f["now"]+timedelta(minutes=11)}
    with store.write() as uow:
        with pytest.raises(InvalidArgument): uow.video.write_manual_annotations([annotation(f,**patch)],now=f["now"])


def test_annotation_batch_conflict_rolls_back_only_batch_and_versions_cannot_skip(fx):
    store,f = fx
    with store.write() as uow:
        uow.video.write_manual_annotations([annotation(f)],now=f["now"])
        with pytest.raises(Conflict):uow.video.write_manual_annotations([annotation(f,sample_key="new"),annotation(f,entries=11)],now=f["now"])
        assert uow.video.list_manual_annotations(f["video"]["id"])["total"] == 1
        with pytest.raises(Conflict):uow.video.write_manual_annotations([annotation(f,version=3)],now=f["now"])
    with store.read() as uow:
        assert uow.video.list_manual_annotations(f["video"]["id"])["items"][0]["entries"] == 10


def test_annotation_requires_current_authorization_and_write_transaction(fx):
    store,f = fx
    with store.read() as uow:
        with pytest.raises(InvalidArgument):uow.video.write_manual_annotations([annotation(f)])
    with store.write() as uow:
        uow.session.get(PermissionRecord,f["permission"]["id"]).valid_until=f["now"].replace(tzinfo=None)
        with pytest.raises(InvalidPermission):uow.video.write_manual_annotations([annotation(f)],now=f["now"])


def test_annotation_external_transaction_rollback(fx):
    store,f = fx
    with pytest.raises(RuntimeError):
        with store.write() as uow:
            uow.video.write_manual_annotations([annotation(f)],now=f["now"])
            raise RuntimeError("caller fails")
    with store.read() as uow:assert uow.video.list_manual_annotations(f["video"]["id"])["total"] == 0


def test_counts_legacy_shape_full_window_filter_and_empty_range(fx):
    store,f = fx
    ready_counts(store,f)
    with store.read() as uow:
        raw = uow.video.query_counts(f["video"]["id"],f["analysis"]["id"])
        assert set(raw)=={"analysis","points"} and len(raw["points"])==6
        selected = uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],start=f["now"]+timedelta(minutes=5),end=f["now"]+timedelta(minutes=15))
        assert [point["entries"] for point in selected["points"]]==[2,3]
        partial = uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],start=f["now"]+timedelta(minutes=1),end=f["now"]+timedelta(minutes=14))
        assert [point["entries"] for point in partial["points"]]==[2]
        assert uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],start=f["now"]+timedelta(days=1))["points"] == []


def test_aggregation_complete_aligned_and_missing_not_zero(fx):
    store,f = fx
    ready_counts(store,f,positions=(0,1,2,3,5))
    with store.read() as uow:
        aggregate=uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],interval_minutes=15)
        assert len(aggregate["points"])==1
        assert aggregate["points"][0]["entries"]==6 and aggregate["points"][0]["exits"]==12
        assert aggregate["points"][0]["quality_status"]=="verified"
        assert aggregate["aggregation"]["expected_window_count"]==2
        assert aggregate["aggregation"]["missing_windows"][0]["available_windows"]==2
        assert not aggregate["aggregation"]["incomplete_boundary_omitted"]
        selected=uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],start=f["now"]+timedelta(minutes=1),end=f["now"]+timedelta(minutes=30),interval_minutes=15)
        assert selected["points"]==[] and selected["aggregation"]["incomplete_boundary_omitted"]


@pytest.mark.parametrize("status",["pending_review","rejected"])
def test_aggregation_does_not_upgrade_unverified_quality(fx,status):
    store,f = fx
    ready_counts(store,f,qualities={1:status})
    with store.read() as uow:
        result=uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],interval_minutes=15)
        assert result["points"][0]["quality_status"]==status
        assert result["points"][1]["quality_status"]=="verified"


@pytest.mark.parametrize("interval",[0,-1,True,1.2,"15",7,1445])
def test_aggregation_bad_interval(fx,interval):
    store,f = fx
    ready_counts(store,f,positions=())
    with store.read() as uow:
        with pytest.raises(InvalidArgument):uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],interval_minutes=interval)


def test_counts_unfinished_and_wrong_video_rejected(fx):
    store,f = fx
    with store.read() as uow:
        with pytest.raises(AnalysisNotReady):uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],interval_minutes=15)
        with pytest.raises(NotFound):uow.video.query_counts(str(uuid4()),f["analysis"]["id"])


def test_aggregation_omits_irregular_native_windows(fx):
    store,f = fx
    with store.write() as uow:
        uow.video.write_counts(f["analysis"]["id"],[CountInput(f["now"],f["now"]+timedelta(minutes=4),3,1)],f["video_lease"])
        uow.video.complete_analysis(f["analysis"]["id"],1,f["video_lease"])
    with store.read() as uow:
        result=uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],interval_minutes=5)
        assert result["points"]==[] and result["aggregation"]["ignored_non_native_windows"]==1
        assert len(result["aggregation"]["missing_windows"])==2


def test_count_query_limit_and_bad_time(fx):
    store,f = fx
    ready_counts(store,f,positions=())
    with store.write() as uow:uow.session.get(Video,f["video"]["id"]).recorded_end_at=(f["now"]+timedelta(days=90)).replace(tzinfo=None)
    with store.read() as uow:
        with pytest.raises(QueryLimitExceeded):uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],interval_minutes=5)
        with pytest.raises(InvalidArgument):uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],start=f["now"],end=f["now"])
        with pytest.raises(InvalidArgument):uow.video.query_counts(f["video"]["id"],f["analysis"]["id"],start=f["now"].replace(tzinfo=None))


def history_data(f,**patch):
    return {"entity_id":f["scenic_id"],"metric_code":"scenic_visits","grain":"month","training_start":date(2025,1,1),
        "training_end":date(2025,12,31),"horizon":2,"model_version":"synthetic-not-a-model","model_config":{"synthetic":True},
        "input_manifest":{"synthetic":True},"input_sha256":"a"*64,"status":"ready",**patch}


def test_history_forecast_get_sorted_points_precision_and_insufficient(fx):
    store,f = fx
    seed_catalog(store)
    points=[{"period_start":date(2026,m,1),"period_end":date(2026,m,28),"value":Decimal("12.3456"),"lower_bound":Decimal("10"),"upper_bound":Decimal("20")} for m in (2,1)]
    with store.write() as uow:
        saved=uow.forecasts.save_history_forecast(history_data(f),points)
        insufficient=uow.forecasts.save_history_forecast(history_data(f,status="data_insufficient",reason="synthetic insufficient"),[])
    with store.read() as uow:
        result=uow.forecasts.get_history_forecast(saved["run"]["id"])
        assert result["run"]["input_manifest"]=={"synthetic":True}
        assert [x["period_start"].month for x in result["points"]]==[1,2]
        assert result["points"][0]["value"]==Decimal("12.3456")
        empty=uow.forecasts.get_history_forecast(insufficient["run"]["id"])
        assert empty["run"]["reason"]=="synthetic insufficient" and empty["points"]==[]
        with pytest.raises(NotFound):uow.forecasts.get_history_forecast(str(uuid4()))


def test_short_forecast_get_sorted_utc_points_and_insufficient(fx):
    store,f = fx
    ready_counts(store,f)
    cutoff=f["now"]+timedelta(minutes=30)
    data={"analysis_id":f["analysis"]["id"],"cutoff_at":cutoff,"direction":"entries","horizon_minutes":15,"step_minutes":5,
        "model_version":"synthetic-not-a-model","model_config":{},"input_manifest":{"counts":[]},"input_sha256":"a"*64,"status":"ready"}
    points=[{"window_start":cutoff+timedelta(minutes=5*i),"window_end":cutoff+timedelta(minutes=5*(i+1)),"value":Decimal("1.2345"),"lower_bound":None,"upper_bound":None} for i in (2,0,1)]
    with store.write() as uow:
        saved=uow.forecasts.save_short_forecast(data,points)
        insufficient=uow.forecasts.save_short_forecast({**data,"status":"data_insufficient","reason":"synthetic insufficient"},[])
    with store.read() as uow:
        result=uow.forecasts.get_short_forecast(saved["run"]["id"])
        assert result["run"]["cutoff_at"]==cutoff
        assert [p["window_start"] for p in result["points"]]==[cutoff+timedelta(minutes=5*i) for i in range(3)]
        assert result["points"][0]["value"]==Decimal("1.2345")
        assert uow.forecasts.get_short_forecast(insufficient["run"]["id"])["points"]==[]
        with pytest.raises(NotFound):uow.forecasts.get_short_forecast(str(uuid4()))
