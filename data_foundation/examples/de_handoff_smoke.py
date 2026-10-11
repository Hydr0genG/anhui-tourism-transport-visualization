"""C→D/E 存储联调样例；所有对象、文档和计数均为合成测试资料。

默认新建临时 SQLite，走五段 Alembic 迁移，结束后自动清理。
MySQL 模式仅接受 TEST_DATABASE_URL 指向名称以 _test 结尾的空库。
不运行真实登录验证、RAG、YOLO/OpenVINO 或预测算法。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
from uuid import uuid4

from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from data_foundation import (
    AnalysisNotReady, AssetCreate, Conflict, CountInput, DataStore,
    EntityCreate, InvalidArgument, JobCreate, JobLease, LeaseLost,
    MetricCreate, SeriesQuery, SessionCreate, SourceCreate, UnsupportedGrain,
    UserCreate, __version__, create_store,
)
from data_foundation.ingestion import import_standard_csv


ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def expect_error(error_type: type[Exception], action: Callable[[], Any]) -> None:
    try:
        action()
    except error_type:
        return
    raise AssertionError(f"预期 {error_type.__name__}，实际没有抛出该错误")


def migrate(database_url: str) -> None:
    environment = dict(os.environ)
    environment["DATABASE_URL"] = database_url
    environment.pop("DATA_FOUNDATION_MIGRATION_STAGE", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=ROOT, env=environment, text=True, capture_output=True,
    )
    if result.returncode:
        # 不把可能包含数据库地址的第三方异常直接输出给接收方。
        raise RuntimeError("迁移失败；请检查测试数据库权限、依赖及迁移版本")


def make_fixture(store: DataStore) -> dict[str, str]:
    ids = {key: str(uuid4()) for key in ("scenic", "camera", "source")}
    metrics = json.loads((ROOT / "assets/metric_dictionary.json").read_text(encoding="utf-8"))
    with store.write() as writer:
        for item in metrics["metrics"]:
            writer.catalog.upsert_metric(MetricCreate(**item))
        writer.catalog.create_entity(EntityCreate(
            id=ids["scenic"], name="DE 合成测试景区", type="scenic", region_code="340000",
        ))
        writer.catalog.create_entity(EntityCreate(
            id=ids["camera"], name="DE 合成测试入口摄像头", type="camera",
            region_code="340000", parent_entity_id=ids["scenic"],
        ))
        writer.provenance.register_source(SourceCreate(
            id=ids["source"], publisher="合成测试发布方", title="仅限测试库的资料来源",
            url="https://example.invalid/de-handoff", usage_note="合成资料，仅验证存储接口",
            update_frequency="monthly", native_grain="month", verification_status="verified",
        ))
    return ids


def save_asset(store: DataStore, source_id: str, name: str, content: bytes, media: str) -> dict[str, Any]:
    # 文件先完整落盘，随后用短事务登记引用。
    staged = store.persist_file(source_id=source_id, original_name=name, stream=io.BytesIO(content))
    with store.write() as writer:
        asset = writer.provenance.register_asset(AssetCreate(
            source_id=source_id, url=f"https://example.invalid/{name}", title=f"合成测试 {name}",
            retrieved_at=utc_now(), published_at=utc_now(), media_type=media,
            usage_note="仅验证接口，不是真实业务资料", **staged,
        ))
    return asset


def d_auth(store: DataStore, ids: dict[str, str], passed: Callable[..., None]) -> None:
    username = "de_test_" + uuid4().hex[:12]
    session_hash = digest(uuid4().bytes)
    now = utc_now()
    with store.write() as writer:
        user = writer.auth.create_user(UserCreate(
            username, "DE 存储测试用户", "TEST_ONLY_NOT_A_VALID_PASSWORD_HASH",
        ))
        writer.auth.save_session(SessionCreate(session_hash, user["id"], now + timedelta(hours=1)), now)
    with store.read() as reader:
        auth_user = reader.auth.get_user_by_username(username)
        session = reader.auth.get_active_session(session_hash, now)
        require(auth_user is not None and "password_hash" in auth_user, "内部认证查询应返回密码哈希")
        require(session is not None and session["user"]["id"] == user["id"], "会话需关联正确用户")
        require("password_hash" not in session["user"], "会话公开用户不应包含密码哈希")
        require(reader.auth.get_active_session(session_hash, now + timedelta(hours=2)) is None, "到期会话应不可用")
    with store.write() as writer:
        require(writer.auth.revoke_session(session_hash, utc_now()) is True, "首次撤销应返回 True")
        require(writer.auth.revoke_session(session_hash, utc_now()) is False, "重复撤销应返回 False")
    with store.read() as reader:
        require(reader.auth.get_active_session(session_hash, utc_now()) is None, "撤销会话应不可用")
    passed("D-auth", "用户查询、会话保存/到期/撤销通过；未验证密码算法或 HTTP 登录")

    def readonly_write() -> None:
        with store.read() as reader:
            reader.auth.revoke_session(session_hash, utc_now())
    expect_error(InvalidArgument, readonly_write)
    passed("D-readonly", "只读工作单元拒绝受约束写入")

    def rollback_write() -> None:
        with store.write() as writer:
            writer.catalog.create_entity(EntityCreate(
                id=str(uuid4()), name="必须回滚的测试对象", type="scenic", region_code="340000",
            ))
            raise RuntimeError("合成回滚场景")
    expect_error(RuntimeError, rollback_write)
    with store.read() as reader:
        require(reader.catalog.list_entities(q="必须回滚的测试对象")["total"] == 0, "异常退出必须回滚")
        require(len(reader.catalog.list_metrics()) == 5, "应有五项固定指标")
    passed("D-transaction", "异常回滚和五指标查询通过")


def csv_content(entity_id: str, value: str) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.writer(handle)
    writer.writerow([
        "entity_id", "metric_code", "period_start", "period_end", "grain", "original_value",
        "original_unit", "standard_value", "standard_unit", "conversion_factor", "source_locator",
        "methodology_version", "published_at", "quality_status",
    ])
    writer.writerow([
        entity_id, "scenic_visits", "2026-01-01", "2026-01-31", "month", value, "人次",
        value, "人次", "1", "合成 CSV 第 2 行", "test-only-v1", "2026-02-10T09:00:00+08:00", "verified",
    ])
    return handle.getvalue().encode("utf-8")


def demo_metric_query(store: DataStore, allowed_entity: str) -> Callable[..., dict[str, Any]]:
    """展示 D 的包装职责；真实白名单由 C/D 根据业务资料另行提供。"""
    def query(*, entity_id: str, metric_code: str, grain: str, start: date, end: date) -> dict[str, Any]:
        if (entity_id, metric_code, grain) != (allowed_entity, "scenic_visits", "month"):
            raise InvalidArgument("超出合成样例查询白名单")
        if start < date(2026, 1, 1) or end > date(2026, 3, 31) or end < start:
            raise InvalidArgument("超出合成样例时间范围")
        with store.read() as reader:
            return reader.observations.query_series(SeriesQuery(entity_id, metric_code, grain, start, end))
    return query


def d_series_and_forecast(store: DataStore, ids: dict[str, str], passed: Callable[..., None]) -> None:
    require(store.data_root is not None, "样例必须配置 data_root")
    asset = save_asset(store, ids["source"], "counts-test.csv", csv_content(ids["scenic"], "100"), "text/csv")
    path = store.data_root / asset["relative_path"]
    query = SeriesQuery(ids["scenic"], "scenic_visits", "month", date(2026, 1, 1), date(2026, 3, 31))
    dry = import_standard_csv(store, path, source_id=ids["source"], asset_id=asset["id"])
    with store.read() as reader:
        require(reader.observations.query_series(query)["points"] == [], "dry-run 不应写入观测")
    require(dry["counts"]["imported"] == 1 and dry["dry_run"], "dry-run 应报告预计新增 1")
    passed("D-dry-run", "记录检查批次，预计新增 1，实际观测仍为 0")

    first = import_standard_csv(store, path, source_id=ids["source"], asset_id=asset["id"], dry_run=False)
    repeated = import_standard_csv(store, path, source_id=ids["source"], asset_id=asset["id"], dry_run=False)
    require(first["counts"]["imported"] == 1 and repeated["counts"]["skipped"] == 1, "首次导入与去重应正确")
    with store.read() as reader:
        series = reader.observations.query_series(query)
        require(len(series["points"]) == 1 and len(series["missing_periods"]) == 2, "三个月范围应有一条观测、两个缺期")
        point = series["points"][0]
        require(point["provenance"]["asset_id"] == asset["id"], "查询结果应保留原件 ID")
        require(point["source_ids"] == [ids["source"]], "查询结果应保留来源")
    passed("D-series", "首次新增 1、重复跳过 1，三个月缺 2 期，来源可追溯")

    revised_asset = save_asset(store, ids["source"], "counts-test-revised.csv", csv_content(ids["scenic"], "110"), "text/csv")
    revised = import_standard_csv(store, store.data_root / revised_asset["relative_path"],
                                  source_id=ids["source"], asset_id=revised_asset["id"], dry_run=False)
    with store.read() as reader:
        current = reader.observations.query_series(query)["points"][0]
    require(revised["counts"]["revised"] == 1 and current["version"] == 2, "修订应递增版本")
    require(str(current["value"]) == "110.0000", "修订后应能查询到新值")
    passed("D-revision", "修订 1 条，查询版本为 2；旧快照由原有自动化测试检查")

    def wrong_grain() -> None:
        with store.read() as reader:
            reader.observations.query_series(SeriesQuery(ids["scenic"], "scenic_visits", "week", query.start, query.end))
    expect_error(UnsupportedGrain, wrong_grain)
    tool = demo_metric_query(store, ids["scenic"])
    require(len(tool(entity_id=ids["scenic"], metric_code="scenic_visits", grain="month", start=query.start, end=query.end)["points"]) == 1,
            "受约束查询应允许白名单请求")
    expect_error(InvalidArgument, lambda: tool(entity_id=ids["scenic"], metric_code="camera_entries", grain="month", start=query.start, end=query.end))
    passed("D-metric-tool", "不支持粒度及白名单拒绝通过；尚未调用 E 的问答函数")

    manifest = {"test_only": True, "observations": [{
        "id": current["id"], "version": current["version"], "value": str(current["value"]),
        "asset_id": revised_asset["id"], "period_end": "2026-01-31",
    }]}
    with store.write() as writer:
        saved = writer.forecasts.save_history_forecast({
            "entity_id": ids["scenic"], "metric_code": "scenic_visits", "grain": "month",
            "training_start": query.start, "training_end": date(2026, 1, 31), "horizon": 1,
            "model_version": "test-only-not-a-model", "model_config": {"test_only": True},
            "input_manifest": manifest, "input_sha256": digest(json_bytes(manifest)), "backtest": None,
            "status": "data_insufficient", "reason": "合成样例仅一条观测，不执行预测算法",
        }, [])
    require(saved["run"]["status"] == "data_insufficient" and saved["points"] == [], "不足状态应无预测点")
    passed("D-history-save", "历史预测不足状态保存通过；未验证预测算法或 run 读取")


def claim(store: DataStore, job_id: str) -> JobLease:
    # v0.1.0 claim_job 没有 type/job_id 过滤；因此只在独占测试库顺序运行。
    with store.write() as writer:
        result = writer.jobs.claim_job("de-handoff-test-worker", utc_now())
        require(result is not None and result["job"]["id"] == job_id, "测试队列不是独占的，不能领取预期任务")
        lease = JobLease(**result["lease"])
        writer.jobs.heartbeat(lease, utc_now())
    return lease


def e_knowledge(store: DataStore, ids: dict[str, str], passed: Callable[..., None]) -> None:
    asset = save_asset(store, ids["source"], "guide-test.md", "# 合成资料\n仅用于验证片段存取。".encode(), "text/markdown")
    now = utc_now()
    with store.write() as writer:
        document = writer.knowledge.register_document({
            "source_id": ids["source"], "asset_id": asset["id"], "title": "合成知识资料",
            "publisher": "合成测试发布方", "source_url": "https://example.invalid/guide-test",
            "published_at": now, "valid_from": None, "valid_until": None,
            "permission_note": "仅存储测试", "document_version": "test-v1",
        }, [ids["scenic"]])
        job = writer.jobs.enqueue_or_get(JobCreate(
            "knowledge_index", digest(uuid4().bytes), {"document_id": document["id"], "test_only": True}, "DE 样例索引",
        ), now)
    with store.read() as reader:
        require(reader.knowledge.get_chunks(document["id"])["status"] == "index_not_ready", "索引初始状态应未就绪")
    lease = claim(store, job["id"])
    with store.write() as writer:
        index = writer.knowledge.begin_index(
            document_id=document["id"], version="test-index-v1", job_id=job["id"],
            parser_version="test-parser-v1", embedding_model_version="none-storage-test", lease=lease,
        )
        chunks = [
            {"chunk_no": 0, "text": "合成片段一", "locator": {"heading": "合成资料", "line": 1}},
            {"chunk_no": 1, "text": "合成片段二", "locator": {"heading": "合成资料", "line": 2}},
        ]
        require(writer.knowledge.write_chunks(index["id"], chunks, lease) == {"inserted": 2, "skipped": 0}, "应写入两个片段")
        require(writer.knowledge.write_chunks(index["id"], chunks, lease) == {"inserted": 0, "skipped": 2}, "相同片段重传应去重")
    passed("E-chunks", "初始未就绪、两片段写入和重复跳过通过")

    def conflicting_chunk() -> None:
        with store.write() as writer:
            writer.knowledge.write_chunks(index["id"], [{"chunk_no": 0, "text": "不同内容", "locator": chunks[0]["locator"]}], lease)
    expect_error(Conflict, conflicting_chunk)
    passed("E-chunk-conflict", "同序号不同文本被拒绝，原片段保持可用")

    # 样例只写一个 JSON 产物，不进行真实向量索引或检索。
    artifact = save_asset(store, ids["source"], "index-test.json", json_bytes({"test_only": True, "chunks": chunks}), "application/json")
    with store.write() as writer:
        writer.knowledge.activate_index(
            document_id=document["id"], version="test-index-v1", artifact_path=artifact["relative_path"],
            artifact_sha256=artifact["sha256"], expected_chunk_count=2, lease=lease,
        )
        writer.jobs.complete_job(lease, {"document_id": document["id"], "index_version": "test-index-v1"}, utc_now())
    with store.read() as reader:
        result = reader.knowledge.get_chunks(document["id"])
        require(result["status"] == "ready" and len(result["items"]) == 2, "激活后应读回两个片段")
        require(reader.jobs.get_job(job["id"])["status"] == "succeeded", "索引作业应成功")
    passed("E-index-publish", "实际 activate_index 与 complete_job 同事务发布并读回")


def e_video(store: DataStore, ids: dict[str, str], passed: Callable[..., None]) -> None:
    now = utc_now()
    start, end = now - timedelta(minutes=10), now
    video_asset = save_asset(store, ids["source"], "video-test.bin", b"TEST_ONLY_NOT_A_REAL_VIDEO", "application/octet-stream")
    evidence = save_asset(store, ids["source"], "permission-test.txt", b"TEST_ONLY_PERMISSION_FIXTURE", "text/plain")
    config = {
        "pipeline_version": "test-only-v1", "detector_model_version": "not-a-real-detector",
        "detector_sha256": digest(b"test-only-weights"), "openvino_version": "not-running",
        "device": "CPU", "tracker_config_id": None, "tracker_config": {"test_only": True},
        "count_line": {"x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5},
        "entry_side": "left", "interval_minutes": 5,
    }
    with store.write() as writer:
        permission = writer.video.register_permission({
            "source_id": ids["source"], "basis": "仅限测试库", "grantor": "合成授权方",
            "evidence_path": evidence["relative_path"], "evidence_sha256": evidence["sha256"],
            "allowed_uses": ["analysis"], "valid_from": now - timedelta(days=1),
            "valid_until": now + timedelta(days=1), "playback_allowed": False, "status": "approved",
        })
        video = writer.video.register_video({
            "source_id": ids["source"], "asset_id": video_asset["id"], "scenic_id": ids["scenic"],
            "camera_id": ids["camera"], "permission_record_id": permission["id"],
            "recorded_start_at": start, "recorded_end_at": end, "camera_location_note": "合成入口",
        }, now)
        spec = JobCreate("video_analysis", digest(uuid4().bytes), {"video_id": video["id"], "test_only": True}, "DE 样例计数")
        job = writer.jobs.enqueue_or_get(spec, now)
        require(writer.jobs.enqueue_or_get(spec, now)["id"] == job["id"], "相同幂等键应复用作业")
        analysis = writer.video.create_analysis(video["id"], job["id"], config)
        require(writer.video.create_analysis(video["id"], job["id"], config)["id"] == analysis["id"], "同视频同配置应复用分析")
    passed("E-video-create", "合成授权/视频/分析登记及作业、配置幂等通过；禁止回放")

    def read_not_ready() -> None:
        with store.read() as reader:
            reader.video.query_counts(video["id"], analysis["id"])
    expect_error(AnalysisNotReady, read_not_ready)
    lease = claim(store, job["id"])
    points = [CountInput(start, start + timedelta(minutes=5), 8, 3), CountInput(start + timedelta(minutes=5), end, 6, 2)]
    with store.write() as writer:
        require(writer.video.write_counts(analysis["id"], points, lease) == {"inserted": 2, "skipped": 0}, "应写入两个窗口")
    expect_error(AnalysisNotReady, read_not_ready)
    with store.write() as writer:
        require(writer.video.write_counts(analysis["id"], points, lease) == {"inserted": 0, "skipped": 2}, "相同计数重传应跳过")
    def conflicting_count() -> None:
        with store.write() as writer:
            writer.video.write_counts(analysis["id"], [CountInput(start, start + timedelta(minutes=5), 999, 3)], lease)
    expect_error(Conflict, conflicting_count)
    passed("E-counts", "未成功时不可对外查询；两窗口写入、重传及冲突拒绝通过")

    with store.write() as writer:
        writer.video.complete_analysis(analysis["id"], 2, lease, manual_validation=None)
        writer.jobs.complete_job(lease, {"analysis_id": analysis["id"]}, utc_now())
    with store.read() as reader:
        result = reader.video.query_counts(video["id"], analysis["id"])
        require([point["entries"] for point in result["points"]] == [8, 6], "成功后应读回合成计数")
        require(reader.jobs.get_job(job["id"])["status"] == "succeeded", "视频作业应成功")
    passed("E-analysis-publish", "complete_analysis 与 complete_job 同事务发布；无真实计数质量结论")

    manifest = {"test_only": True, "counts": [{"window_end": point.window_end.isoformat(), "entries": point.entries} for point in points]}
    data = {
        "analysis_id": analysis["id"], "cutoff_at": end, "direction": "entries",
        "horizon_minutes": 15, "step_minutes": 5, "model_version": "test-only-not-a-model",
        "model_config": {"test_only": True}, "input_manifest": manifest,
        "input_sha256": digest(json_bytes(manifest)), "validation_error": None,
        "status": "data_insufficient", "reason": "合成计数仅两个窗口，不执行预测算法",
    }
    with store.write() as writer:
        forecast = writer.forecasts.save_short_forecast(data, [])
    require(forecast["run"]["status"] == "data_insufficient" and forecast["points"] == [], "不足状态应无结果点")
    def future_input() -> None:
        future_manifest = {"counts": [{"window_end": (end + timedelta(minutes=1)).isoformat()}]}
        with store.write() as writer:
            writer.forecasts.save_short_forecast({**data, "input_manifest": future_manifest, "input_sha256": digest(json_bytes(future_manifest))}, [])
    expect_error(InvalidArgument, future_input)
    passed("E-short-save", "短时预测不足状态可保存，输入越过 cutoff_at 被拒绝")

    def completed_lease() -> None:
        with store.write() as writer:
            writer.jobs.heartbeat(lease, utc_now())
    expect_error(LeaseLost, completed_lease)
    with store.write() as writer:
        failed = writer.jobs.enqueue_or_get(JobCreate("knowledge_index", digest(uuid4().bytes), {"test_only": True}, "合成失败场景"), utc_now())
    failed_lease = claim(store, failed["id"])
    with store.write() as writer:
        writer.jobs.fail_job(failed_lease, "合成失败：未调用模型", utc_now())
    with store.read() as reader:
        require(reader.jobs.get_job(failed["id"])["status"] == "failed", "作业失败应可读回")
    passed("E-job-fail", "完成后租约不可续期、作业失败可读回；未实现自动恢复/重试")


def datetime_precision_checks(store: DataStore, passed: Callable[[str, str], None]) -> None:
    # 固定在会触发 DATETIME(0) 进位的微秒位置，不依赖运行时碰巧落在哪一秒。
    now = utc_now().replace(microsecond=700000)
    with store.write() as writer:
        job = writer.jobs.enqueue_or_get(JobCreate(
            "knowledge_index", digest(uuid4().bytes), {"precision_test": True}, "微秒精度复验"
        ), now)
    with store.read() as reader:
        persisted = reader.jobs.get_job(job["id"])
        require(persisted["available_at"] == now, "任务可领取时间必须完整保留微秒")
    with store.write() as writer:
        claimed = writer.jobs.claim_job("precision-test-worker", now, lease_seconds=1)
        require(claimed is not None and claimed["job"]["id"] == job["id"],
                "带 700000 微秒的任务必须能在同一时刻领取")
    lease = JobLease(**claimed["lease"])
    passed("precision-job", "700000 微秒入队后原值读回，同一时刻领取成功")
    expiry = now + timedelta(seconds=1)
    with store.read() as reader:
        attempt = reader.jobs.ensure_lease(lease, expiry - timedelta(microseconds=1))
        require(attempt.lease_expires_at == expiry.replace(tzinfo=None), "租约到期时间必须保留微秒")
        expect_error(LeaseLost, lambda: reader.jobs.ensure_lease(lease, expiry))
    with store.write() as writer:
        writer.jobs.fail_job(lease, "精度检查结束", now)
    passed("precision-lease", "租约到期前一微秒有效，到期时失效")
    session_hash = digest(uuid4().bytes)
    with store.write() as writer:
        user = writer.auth.create_user(UserCreate("precision_" + uuid4().hex[:12], "精度测试", "test-only"))
        writer.auth.save_session(SessionCreate(session_hash, user["id"], expiry), now)
    with store.read() as reader:
        active = reader.auth.get_active_session(session_hash, expiry - timedelta(microseconds=1))
        require(active is not None, "会话到期前一微秒必须有效")
        require(active["session"]["expires_at"] == expiry, "会话到期时间必须保留微秒")
        require(reader.auth.get_active_session(session_hash, expiry) is None, "会话到期时必须失效")
    passed("precision-session", "会话到期时间保留微秒，边界有效性正确")


def run_suite(database_url: str, data_root: Path, part: str, sqlite: bool) -> dict[str, Any]:
    migrate(database_url)
    store = create_store(database_url, data_root=data_root, allow_sqlite_for_tests=sqlite)
    report: dict[str, Any] = {
        "report_version": "de-handoff/1", "generated_at": utc_now().isoformat(),
        "package_version": __version__,
        "part": part, "scope": "synthetic_storage_only", "checks": [],
    }
    def passed(name: str, note: str) -> None:
        report["checks"].append({"name": name, "status": "passed", "note": note})
    try:
        report["database_dialect"] = store.doctor()["dialect"]
        with store.engine.connect() as connection:
            report["migration_head"] = MigrationContext.configure(connection).get_current_revision()
        require(report["migration_head"] == "0005", "迁移实际 head 应为 0005")
        tables = set(inspect(store.engine).get_table_names())
        require(len(tables - {"alembic_version"}) == 30, "应建立 30 张业务表")
        passed("migration", "五段迁移和连接检查通过，30 张业务表")
        datetime_precision_checks(store, passed)
        ids = make_fixture(store)
        passed("fixture", "仅创建合成来源、景区和摄像头，文件摘要来自实际测试文件")
        if part in {"d", "all"}:
            d_auth(store, ids, passed)
            d_series_and_forecast(store, ids, passed)
        if part in {"e", "all"}:
            e_knowledge(store, ids, passed)
            e_video(store, ids, passed)
        report["status"] = "passed"
        report["check_count"] = len(report["checks"])
        return report
    finally:
        store.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", choices=("d", "e", "all"), default="all")
    parser.add_argument("--target", choices=("local", "mysql-test"), default="local")
    parser.add_argument("--data-root", type=Path, help="mysql-test 需要显式指定空的测试资产目录")
    parser.add_argument("--report", type=Path, help="保存不含数据库地址/哈希/租约 token 的 JSON 报告")
    args = parser.parse_args(argv)
    try:
        if args.target == "local":
            if args.data_root:
                raise ValueError("local 模式自动使用临时目录，无需指定 data-root")
            with TemporaryDirectory(prefix="c-de-handoff-") as temporary:
                root = Path(temporary)
                report = run_suite(f"sqlite+pysqlite:///{root / 'test.sqlite'}", root / "assets", args.part, True)
        else:
            url = os.environ.get("TEST_DATABASE_URL", "")
            if not url:
                raise ValueError("mysql-test 需要 TEST_DATABASE_URL")
            parsed = make_url(url)
            if parsed.drivername != "mysql+pymysql" or not (parsed.database or "").endswith("_test"):
                raise ValueError("只能使用 mysql+pymysql 且库名以 _test 结尾的独占测试库")
            if not args.data_root:
                raise ValueError("mysql-test 需要 --data-root 指定测试资产目录")
            root = args.data_root.resolve()
            if root.exists() and (not root.is_dir() or any(root.iterdir())):
                raise ValueError("测试资产目录必须为空")
            engine = create_engine(url)
            try:
                require(not inspect(engine).get_table_names(), "测试库必须为空，样例不清理或复用已有库")
            finally:
                engine.dispose()
            report = run_suite(url, root, args.part, False)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # 数据库驱动异常可能含连接信息，只输出错误类别；自己抛出的参数/断言可安全解释。
        message = str(exc) if isinstance(exc, (ValueError, AssertionError, RuntimeError)) else "检查未通过，请在独占测试环境核对依赖和输入"
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "message": message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
