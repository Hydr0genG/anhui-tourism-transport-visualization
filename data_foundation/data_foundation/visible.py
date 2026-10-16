"""Lease-protected P2PNet storage. No dependency on E or its model runtime."""
from datetime import datetime, timedelta, timezone
import json
import math
import re
from statistics import median
from uuid import UUID, uuid4

from sqlalchemy import select

from .db.models import Job, Video, VisibleAnalysis, VisibleCountPoint, VisibleForecast
from .errors import AnalysisNotReady, Conflict, InvalidArgument, LeaseLost, NotFound
from .repositories import JobRepository, Repository, VideoRepository, public_columns, sha256_json, utc_aware, utc_naive


def json_copy(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(raw) > 128_000:
            raise ValueError("oversize")
        return json.loads(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidArgument("须为大小受限且数值有限的JSON") from exc


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1e12 or not math.isfinite(value):
        raise InvalidArgument("人数或误差须为有限非负数")
    return float(value)


def stamp(value):
    try:
        return utc_naive(datetime.fromisoformat(value) if isinstance(value, str) else value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidArgument("时间须为带时区ISO时间或datetime") from exc


def config_and_provenance(config, provenance):
    config, provenance = json_copy(config), json_copy(provenance)
    keys = {"count_model", "weights_version", "roi", "sample_interval_seconds", "aggregation_minutes",
            "confidence_threshold", "inference_engine", "device"}
    if not isinstance(config, dict) or set(config) != keys:
        raise InvalidArgument("须提供完整P2PNet原生配置，禁止旧计数线/方向/OpenVINO字段")
    if (config["count_model"] != "p2pnet" or config["inference_engine"] != "pytorch" or config["device"] != "CPU"
            or type(config["confidence_threshold"]) not in (int, float) or config["confidence_threshold"] != 0.5
            or type(config["aggregation_minutes"]) is not int or config["aggregation_minutes"] != 5
            or type(config["sample_interval_seconds"]) is not int or not 1 <= config["sample_interval_seconds"] <= 600
            or not isinstance(config["weights_version"], str) or not 1 <= len(config["weights_version"].strip()) <= 100):
        raise InvalidArgument("仅支持P2PNet/PyTorch/CPU、0.5阈值和5分钟聚合")
    roi = config["roi"]
    if not isinstance(roi, dict) or set(roi) != {"points"} or not isinstance(roi["points"], list) or not 3 <= len(roi["points"]) <= 128:
        raise InvalidArgument("ROI须为归一化多边形")
    points = roi["points"]
    for p in points:
        if not isinstance(p, dict) or set(p) != {"x", "y"} or any(number(p[k]) > 1 for k in ("x", "y")):
            raise InvalidArgument("ROI点须为0至1的x/y")
    xy = [(p["x"], p["y"]) for p in points]
    pairs = list(zip(xy, xy[1:] + xy[:1]))
    if len(set(xy)) != len(xy) or abs(sum(a[0]*b[1] - b[0]*a[1] for a,b in pairs)) < 1e-10:
        raise InvalidArgument("ROI顶点须唯一且面积非零")
    def cross(a,b,c):
        return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
    for i,(a,b) in enumerate(pairs):
        for j,(c,d) in enumerate(pairs):
            if j <= i+1 or (i == 0 and j == len(pairs)-1):
                continue
            overlap = (max(min(a[0],b[0]),min(c[0],d[0])) <= min(max(a[0],b[0]),max(c[0],d[0]))
                       and max(min(a[1],b[1]),min(c[1],d[1])) <= min(max(a[1],b[1]),max(c[1],d[1])))
            if overlap and cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0:
                raise InvalidArgument("ROI不能自交")
    required = {"count_model", "weights_version", "weights_sha256", "model_source_revision",
                "confidence_threshold", "inference_engine", "device"}
    optional = {"sample_interval_seconds", "aggregation_minutes", "torch_version", "cuda_available", "runtime_version"}
    if not isinstance(provenance, dict) or not required <= set(provenance) or set(provenance) - required - optional:
        raise InvalidArgument("模型溯源字段不完整或含未允许字段")
    if (not isinstance(provenance["weights_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", provenance["weights_sha256"])
            or not isinstance(provenance["model_source_revision"], str) or not 1 <= len(provenance["model_source_revision"].strip()) <= 200):
        raise InvalidArgument("权重完整摘要及源码修订必填")
    for key in required & set(config) | optional & set(config) & set(provenance):
        if provenance[key] != config[key]:
            raise InvalidArgument("本次模型溯源与分析配置不一致", field=key)
    if provenance.get("cuda_available") not in (None, False):
        raise InvalidArgument("当前P2PNet不接收CUDA可用环境的CPU声明")
    return config, provenance


class VisibleRepository(Repository):
    def _identity(self, video):
        from .db.models import SourceAsset
        asset = self.get_or_404(SourceAsset, video.asset_id, "录像原件")
        if asset.source_id != video.source_id:
            raise Conflict("录像与原件来源不一致")
        return {"video_id": video.id, "asset_id": asset.id, "source_id": video.source_id,
                "camera_id": video.camera_id, "scenic_id": video.scenic_id,
                "permission_record_id": video.permission_record_id,
                "recorded_start_at": utc_aware(video.recorded_start_at).isoformat(),
                "recorded_end_at": utc_aware(video.recorded_end_at).isoformat(),
                "sha256": asset.sha256, "byte_size": asset.byte_size}

    def _integrity(self, analysis, video):
        if (analysis.input_identity != self._identity(video) or analysis.config_sha256 != sha256_json(
                {"config": analysis.config, "provenance": analysis.provenance, "input_identity": analysis.input_identity})):
            raise Conflict("分析输入/模型/ROI已变化，应新建分析，不得补写旧结果")

    def create_analysis(self, *, video_id, job_id, config, provenance, analysis_id=None, now=None):
        self.require_write()
        config, provenance = config_and_provenance(config, provenance)
        identity = str(uuid4()) if analysis_id is None else analysis_id
        try:
            if str(UUID(identity)) != identity:
                raise ValueError("noncanonical")
        except (TypeError, ValueError, AttributeError) as exc:
            raise InvalidArgument("analysis_id须为规范UUID") from exc
        job = self.locked(Job, job_id, "任务")
        if job.type != "video_analysis" or job.status != "queued":
            raise Conflict("只能为排队中的video_analysis任务登记")
        if job.payload.get("video_id", video_id) != video_id or job.payload.get("analysis_id", identity) != identity:
            raise Conflict("任务载荷与录像/分析身份不一致")
        video = self.locked(Video, video_id, "录像")
        VideoRepository(self.session, True)._permission(video, "analysis", now, lock=True)
        identity_snapshot = self._identity(video)
        digest = sha256_json({"config": config, "provenance": provenance, "input_identity": identity_snapshot})
        old = self.session.scalar(select(VisibleAnalysis).where(VisibleAnalysis.job_id == job_id).with_for_update()
                                  .execution_options(populate_existing=True))
        if old:
            if old.video_id != video_id or old.config_sha256 != digest or (analysis_id is not None and old.id != identity):
                raise Conflict("同一任务的分析登记内容冲突")
            return public_columns(old)
        if self.session.get(VisibleAnalysis, identity):
            raise Conflict("analysis_id已属于其他任务")
        row = VisibleAnalysis(id=identity, video_id=video_id, job_id=job_id, config_sha256=digest,
                              config=config, provenance=provenance, input_identity=identity_snapshot, status="queued")
        self.session.add(row)
        self.session.flush()
        return public_columns(row)

    def _owned(self, analysis_id, lease, now=None):
        self.require_write()
        JobRepository(self.session, True).ensure_lease(lease, now)
        initial = self.get_or_404(VisibleAnalysis, analysis_id, "画面人数分析")
        video = self.locked(Video, initial.video_id, "录像")
        analysis = self.locked(VisibleAnalysis, analysis_id, "画面人数分析")
        if analysis.job_id != lease.job_id:
            raise LeaseLost("租约不属于该画面人数分析")
        self._integrity(analysis, video)
        VideoRepository(self.session, True)._permission(video, "analysis", now, lock=True)
        return analysis, video

    def input_metadata(self, analysis_id, lease, now=None):
        analysis, video = self._owned(analysis_id, lease, now)
        if analysis.status not in {"queued", "running", "failed"}:
            raise Conflict("已发布分析不再领取输入")
        from .db.models import SourceAsset
        asset = self.get_or_404(SourceAsset, video.asset_id, "录像原件")
        if asset.source_id != video.source_id:
            raise Conflict("录像原件与来源不一致")
        return {"analysis_id": analysis.id, "video_id": video.id, "asset_id": asset.id,
                "source_ids": [video.source_id], "config": json_copy(analysis.config),
                "config_sha256": analysis.config_sha256, "recorded_start_at": utc_aware(video.recorded_start_at).isoformat(),
                "recorded_end_at": utc_aware(video.recorded_end_at).isoformat(), "relative_path": asset.relative_path,
                "sha256": asset.sha256, "byte_size": asset.byte_size, "media_type": asset.media_type}

    def begin_analysis(self, analysis_id, lease, now=None):
        analysis, video = self._owned(analysis_id, lease, now)
        if analysis.status not in {"queued", "running", "failed"}:
            raise Conflict("已完成画面人数分析不能重开")
        analysis.status, analysis.error_summary = "running", None
        if video.status != "ready":
            video.status = "processing"
        self.session.flush()
        return public_columns(analysis)

    @staticmethod
    def _payload(row):
        return {"metric": "camera_visible_people", "unit": "人", "grain": row.grain,
                "recorded_at": utc_aware(row.recorded_at).isoformat() if row.recorded_at else None,
                "period_start": utc_aware(row.period_start).isoformat() if row.period_start else None,
                "period_end": utc_aware(row.period_end).isoformat() if row.period_end else None,
                "value": row.value, "sample_count": row.sample_count, "quality_status": row.quality_status,
                "aggregation_rule": "median" if row.grain == "5min" else None, "source_ids": row.source_ids,
                "published_at": utc_aware(row.published_at).isoformat() if row.published_at else None,
                "ingested_at": utc_aware(row.ingested_at).isoformat() if row.ingested_at else None}

    def _point(self, item, analysis, video):
        required = {"grain", "value", "sample_count", "quality_status", "source_ids"}
        allowed = required | {"metric", "unit", "recorded_at", "period_start", "period_end", "aggregation_rule", "published_at", "ingested_at"}
        if not isinstance(item, dict) or not required <= set(item) or set(item) - allowed:
            raise InvalidArgument("人数点字段不完整或含未知字段")
        if item.get("metric", "camera_visible_people") != "camera_visible_people" or item.get("unit", "人") != "人":
            raise InvalidArgument("画面人数不能用进出人次承载")
        if (type(item["sample_count"]) is not int or not 0 <= item["sample_count"] <= 1_000_000
                or item["quality_status"] not in ("good", "degraded", "unavailable")
                or item["source_ids"] != [video.source_id]):
            raise InvalidArgument("有效样本数/质量/来源无效")
        value = None if item["value"] is None else number(item["value"])
        if ((value is None) != (item["quality_status"] == "unavailable")
                or value is not None and item["sample_count"] < 1 or value is None and item["sample_count"] != 0):
            raise InvalidArgument("缺失必须是null/unavailable/0样本，不能补零")
        row = VisibleCountPoint(analysis_id=analysis.id, grain=item["grain"], value=value,
            sample_count=item["sample_count"], quality_status=item["quality_status"], source_ids=list(item["source_ids"]))
        if item["grain"] == "frame":
            row.recorded_at = stamp(item.get("recorded_at"))
            delta = (row.recorded_at - video.recorded_start_at).total_seconds()
            interval = analysis.config["sample_interval_seconds"]
            slot = math.floor(delta / interval)
            if (item.get("period_start") is not None or item.get("period_end") is not None or item.get("aggregation_rule") is not None
                    or row.recorded_at < video.recorded_start_at or row.recorded_at >= video.recorded_end_at
                    or delta-slot*interval > min(1, interval/2)
                    or item["sample_count"] != (0 if value is None else 1)):
                raise InvalidArgument("帧时刻、采样槽或帧样本数无效")
            row.point_key = sha256_json(["frame", slot])
        elif item["grain"] == "5min":
            row.period_start, row.period_end = stamp(item.get("period_start")), stamp(item.get("period_end"))
            offset = (row.period_start - video.recorded_start_at).total_seconds()
            if (item.get("recorded_at") is not None or item.get("aggregation_rule") != "median"
                    or row.period_end-row.period_start != timedelta(minutes=5) or offset < 0 or offset % 300 != 0
                    or row.period_end > video.recorded_end_at):
                raise InvalidArgument("聚合须为自真实录像起点对齐的完整5分钟中位数窗口")
            row.point_key = sha256_json(["5min", int(offset // 300)])
        else:
            raise InvalidArgument("仅支持frame和5min原生粒度")
        for key in ("published_at", "ingested_at"):
            if item.get(key) is not None:
                setattr(row, key, stamp(item[key]))
        row.content_sha256 = sha256_json(self._payload(row))
        return row

    def write_points(self, analysis_id, points, lease, now=None):
        if not isinstance(points, list) or len(points) > 1000:
            raise InvalidArgument("人数点单批最多1000条")
        with self.session.begin_nested():
            analysis, video = self._owned(analysis_id, lease, now)
            if analysis.status != "running":
                raise Conflict("先begin_analysis，完成/失败后不能直接写入")
            parsed = [self._point(p, analysis, video) for p in points]
            existing = {r.point_key: r for r in self.session.scalars(select(VisibleCountPoint).where(
                VisibleCountPoint.analysis_id == analysis_id).with_for_update().execution_options(populate_existing=True))}
            counts = {"inserted": 0, "skipped": 0}
            for row in parsed:
                if row.point_key in existing:
                    if existing[row.point_key].content_sha256 != row.content_sha256:
                        raise Conflict("同采样槽或窗口的结果冲突；不删除旧点")
                    counts["skipped"] += 1
                else:
                    self.session.add(row)
                    existing[row.point_key] = row
                    counts["inserted"] += 1
            JobRepository(self.session, True).ensure_lease(lease, now)
            self.session.flush()
            return counts

    def complete_analysis(self, analysis_id, expected_point_count, lease, manual_validation=None, now=None):
        JobRepository.validate_count(expected_point_count, "expected_point_count", positive=True)
        manual_validation = json_copy(manual_validation)
        if manual_validation is not None and (not isinstance(manual_validation, dict)
                or set(manual_validation) != {"sample_count", "mae"}
                or type(manual_validation["sample_count"]) is not int or manual_validation["sample_count"] < 1):
            raise InvalidArgument("未评估须为null；真实人工评估须有正样本数及MAE")
        if manual_validation is not None:
            number(manual_validation["mae"])
        with self.session.begin_nested():
            analysis, video = self._owned(analysis_id, lease, now)
            if analysis.status != "running":
                raise Conflict("只能发布running分析")
            rows = list(self.session.scalars(select(VisibleCountPoint).where(VisibleCountPoint.analysis_id == analysis_id)
                .with_for_update().execution_options(populate_existing=True)))
            if len(rows) != expected_point_count:
                raise Conflict("人数点数与预期不一致")
            for row in rows:
                if sha256_json(self._payload(row)) != row.content_sha256:
                    raise Conflict("已写人数点内容与摘要不一致")
            frames = [r for r in rows if r.grain == "frame" and r.value is not None]
            windows = [r for r in rows if r.grain == "5min"]
            if len(windows) != int((video.recorded_end_at-video.recorded_start_at).total_seconds() // 300):
                raise Conflict("完整5分钟窗口未全部提交，缺失窗口也应显式unavailable")
            interval = analysis.config["sample_interval_seconds"]
            for row in windows:
                samples = [r.value for r in frames if row.period_start <= r.recorded_at < row.period_end]
                start = (row.period_start-video.recorded_start_at).total_seconds()
                expected = math.ceil((start+300)/interval) - math.ceil(start/interval)
                quality = "unavailable" if not samples else "good" if len(samples) == expected else "degraded"
                if (row.sample_count != len(samples) or row.value != (float(median(samples)) if samples else None)
                        or row.quality_status != quality):
                    raise Conflict("窗口中位数/样本数/质量与本次帧结果不一致")
            JobRepository(self.session, True).ensure_lease(lease, now)
            analysis.status, analysis.manual_validation, analysis.error_summary = "succeeded", manual_validation, None
            video.status = "ready"
            result = {"analysis_id": analysis.id, "video_id": video.id, "metric": "camera_visible_people", "point_count": len(rows)}
            JobRepository(self.session, True).complete_job(lease, result, now or datetime.now(timezone.utc))
            self.session.flush()
            return public_columns(analysis)

    def fail_analysis(self, analysis_id, error_summary, lease, now=None):
        if not isinstance(error_summary, str) or not error_summary.strip():
            raise InvalidArgument("失败原因不能为空")
        with self.session.begin_nested():
            analysis, video = self._owned(analysis_id, lease, now)
            if analysis.status == "succeeded":
                raise Conflict("已发布分析不能改失败")
            analysis.status, analysis.error_summary = "failed", error_summary[:2000]
            if video.status != "ready":
                video.status = "failed"
            JobRepository(self.session, True).fail_job(lease, error_summary[:2000], now or datetime.now(timezone.utc))
            self.session.flush()

    def _readable(self, analysis_id, video_id=None, now=None):
        row = self.get_or_404(VisibleAnalysis, analysis_id, "画面人数分析")
        if video_id is not None and row.video_id != video_id:
            raise NotFound("分析不属于该录像")
        video = self.get_or_404(Video, row.video_id, "录像")
        self._integrity(row, video)
        VideoRepository(self.session, False)._permission(video, "analysis", now)
        return row, video

    def get_analysis(self, analysis_id, now=None):
        row, _ = self._readable(analysis_id, now=now)
        return public_columns(row)

    def get_counts(self, video_id, analysis_id, now=None):
        row, video = self._readable(analysis_id, video_id, now)
        ready = row.status == "succeeded"
        points = list(self.session.scalars(select(VisibleCountPoint).where(VisibleCountPoint.analysis_id == analysis_id)
            .order_by(VisibleCountPoint.grain, VisibleCountPoint.recorded_at, VisibleCountPoint.period_start))) if ready else []
        for p in points:
            if sha256_json(self._payload(p)) != p.content_sha256:
                raise Conflict("人数结果内容校验失败")
        return {"video_id": video_id, "analysis_id": analysis_id, "metric": "camera_visible_people",
                "status": "ready" if ready else "analysis_not_ready", "points": [self._payload(p) for p in points],
                "recorded_start_at": utc_aware(video.recorded_start_at).isoformat(),
                "recorded_end_at": utc_aware(video.recorded_end_at).isoformat(),
                "roi": json_copy(row.config["roi"]) if ready else None,
                "analysis": json_copy(row.config) if ready else None,
                "manual_validation": json_copy(row.manual_validation) if ready else None}

    def forecast_input(self, analysis_id, cutoff_at, now=None):
        analysis, video = self._readable(analysis_id, now=now)
        if analysis.status != "succeeded":
            raise AnalysisNotReady("画面人数分析尚未发布")
        cutoff = stamp(cutoff_at)
        if not video.recorded_start_at <= cutoff <= video.recorded_end_at:
            raise InvalidArgument("cutoff须在真实录像范围内")
        rows = list(self.session.scalars(select(VisibleCountPoint).where(VisibleCountPoint.analysis_id == analysis_id,
            VisibleCountPoint.grain == "5min", VisibleCountPoint.period_end <= cutoff).order_by(VisibleCountPoint.period_end)))
        for r in rows:
            if sha256_json(self._payload(r)) != r.content_sha256:
                raise Conflict("预测输入人数点校验失败")
        manifest = {"analysis_id": analysis_id, "video_id": video.id, "config_sha256": analysis.config_sha256,
                    "cutoff_at": utc_aware(cutoff).isoformat(),
                    "points": [{"id": r.id, "sha256": r.content_sha256} for r in rows]}
        return {"input_manifest": manifest, "input_sha256": sha256_json(manifest),
                "history": [{"at": utc_aware(r.period_end).isoformat(),
                             "value": r.value if r.quality_status == "good" else None} for r in rows]}

    def publish_forecast(self, *, analysis_id, cutoff_at, horizon_minutes, input_sha256, result, lease, now=None):
        self.require_write()
        if type(horizon_minutes) is not int or horizon_minutes not in (15,30,60):
            raise InvalidArgument("预测跨度只能是15/30/60；60仅存不足状态")
        result = json_copy(result)
        allowed = {"metric", "status", "forecast_points", "training_cutoff", "model_version", "input_window_minutes", "validation_error", "reason"}
        if (not isinstance(result, dict) or set(result) - allowed or not {"metric","status","forecast_points","model_version"} <= set(result)
                or result["metric"] != "camera_visible_people" or result["status"] not in ("ready","data_insufficient","quality_insufficient")
                or not isinstance(result["model_version"], str) or not 1 <= len(result["model_version"].strip()) <= 100
                or not isinstance(result["forecast_points"], list)):
            raise InvalidArgument("预测结果结构或指标无效")
        cutoff = stamp(cutoff_at)
        if result.get("training_cutoff") is not None and stamp(result["training_cutoff"]) != cutoff:
            raise InvalidArgument("预测cutoff绑定不一致")
        if result["status"] != "ready":
            if result["forecast_points"] or not isinstance(result.get("reason"), str) or not result["reason"].strip():
                raise InvalidArgument("不足状态须空预测及明确原因")
        elif horizon_minutes == 60 or len(result["forecast_points"]) != horizon_minutes // 5:
            raise InvalidArgument("60分钟未开放或预测点数不正确")
        for i, point in enumerate(result["forecast_points"], 1):
            if (not isinstance(point, dict) or set(point) - {"target_time","value","is_forecast","lower","upper"}
                    or not {"target_time","value","is_forecast"} <= set(point) or point["is_forecast"] is not True
                    or stamp(point["target_time"]) != cutoff + timedelta(minutes=5*i)):
                raise InvalidArgument("预测点时间或字段无效")
            value = number(point["value"])
            lower, upper = point.get("lower"), point.get("upper")
            if (lower is None) != (upper is None) or lower is not None and not number(lower) <= value <= number(upper):
                raise InvalidArgument("预测上下界须同时为空或有序包围预测值")
        with self.session.begin_nested():
            JobRepository(self.session, True).ensure_lease(lease, now)
            job = self.locked(Job, lease.job_id, "预测任务")
            if (job.type != "visible_forecast" or job.payload.get("analysis_id") != analysis_id
                    or job.payload.get("horizon_minutes") != horizon_minutes
                    or stamp(job.payload.get("cutoff_at")) != cutoff):
                raise LeaseLost("预测租约与分析身份不一致")
            initial = self.get_or_404(VisibleAnalysis, analysis_id, "画面人数分析")
            video = self.locked(Video, initial.video_id, "录像")
            analysis = self.locked(VisibleAnalysis, analysis_id, "画面人数分析")
            VideoRepository(self.session, True)._permission(video, "analysis", now, lock=True)
            current = self.forecast_input(analysis_id, cutoff_at, now)
            if current["input_sha256"] != input_sha256:
                raise Conflict("预测输入指纹已变化")
            if result["status"] == "ready":
                history = current["history"]
                minimum = 17 if horizon_minutes == 15 else 32
                validation = result.get("validation_error")
                if not isinstance(validation, dict):
                    raise InvalidArgument("ready预测必须有按跨度绑定的回测报告")
                error = validation.get(str(horizon_minutes), {})
                if (result.get("training_cutoff") is None or result.get("input_window_minutes") != len(history)*5
                        or len(history) < minimum or any(p["value"] is None for p in history)
                        or any(stamp(b["at"])-stamp(a["at"]) != timedelta(minutes=5) for a,b in zip(history,history[1:]))
                        or cutoff-stamp(history[-1]["at"]) >= timedelta(minutes=5)
                        or not isinstance(error, dict) or type(error.get("fold_count")) is not int or error["fold_count"] < 3
                        or number(error.get("mae")) > number(error.get("persistence_mae")) + 1e-9):
                    raise InvalidArgument("预测尚无足够连续历史或有效回测，不能标ready")
            row = VisibleForecast(analysis_id=analysis_id, job_id=lease.job_id, cutoff_at=cutoff,
                                  horizon_minutes=horizon_minutes, input_sha256=input_sha256,
                                  input_manifest=current["input_manifest"], result=result)
            self.session.add(row)
            self.session.flush()
            JobRepository(self.session, True).complete_job(lease, {"forecast_id": row.id, "analysis_id": analysis_id}, now or datetime.now(timezone.utc))
            return public_columns(row)

    def get_forecast(self, forecast_id, now=None):
        row = self.get_or_404(VisibleForecast, forecast_id, "画面人数预测")
        self._readable(row.analysis_id, now=now)
        return public_columns(row)
