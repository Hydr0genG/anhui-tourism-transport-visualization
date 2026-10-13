from __future__ import annotations

import calendar
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import LargeBinary, and_, cast, func, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .contracts import (
    AssetCreate,
    CountInput,
    EntityCreate,
    JobCreate,
    JobLease,
    MetricCreate,
    ObservationInput,
    Page,
    SeriesQuery,
    SessionCreate,
    SourceCreate,
    UserCreate,
)
from .db.base import utcnow_naive
from .db.models import (
    CameraCount,
    DocumentEntity,
    Entity,
    ForecastPoint,
    ForecastRun,
    Holiday,
    IngestionRun,
    Job,
    JobAttempt,
    JobRetry,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndex,
    ManualAnnotation,
    Metric,
    Event,
    EventEntity,
    EventHistory,
    Observation,
    ObservationRevision,
    PermissionRecord,
    QualityIssue,
    SeriesPolicy,
    SessionRecord,
    ShortForecastPoint,
    ShortForecastRun,
    Source,
    SourceAsset,
    User,
    Video,
    VideoAnalysis,
)
from .errors import (
    AnalysisNotReady,
    Conflict,
    InvalidArgument,
    InvalidPermission,
    LeaseLost,
    MetricEntityMismatch,
    NotFound,
    QueryLimitExceeded,
    UnsupportedGrain,
)


ALLOWED_ENTITY_TYPES = {"scenic", "rail_station", "airport", "camera"}
ALLOWED_GRAINS = {"day", "week", "month", "holiday"}
ALLOWED_QUALITY = {"verified", "pending_review", "rejected"}


def utc_naive(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidArgument("时间必须带时区", field="datetime")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def public_columns(model: Any, *, exclude: set[str] | None = None) -> dict[str, Any]:
    excluded = exclude or set()
    result: dict[str, Any] = {}
    for column in model.__table__.columns:
        if column.name in excluded:
            continue
        value = getattr(model, column.name)
        if isinstance(value, datetime):
            value = utc_aware(value)
        result[column.name] = value
    return result


class Repository:
    def __init__(self, session: Session, writable: bool, data_root: Path | None = None) -> None:
        self.session = session
        self.writable = writable
        self.data_root = data_root

    def locked(self, model: type[Any], key: str, label: str) -> Any:
        value = self.session.scalar(select(model).where(model.id == key)
            .with_for_update().execution_options(populate_existing=True))
        if value is None:
            raise NotFound(f"{label}不存在", id=key)
        return value

    def require_write(self) -> None:
        if not self.writable:
            raise InvalidArgument("当前工作单元为只读")

    def get_or_404(self, model: type[Any], key: Any, label: str) -> Any:
        value = self.session.get(model, key)
        if value is None:
            raise NotFound(f"{label}不存在", id=str(key))
        return value


class AuthRepository(Repository):
    def create_user(self, data: UserCreate) -> dict[str, Any]:
        self.require_write()
        if data.role not in {"viewer", "admin"}:
            raise InvalidArgument("role 只能是 viewer 或 admin")
        if not 3 <= len(data.username) <= 64 or not data.password_hash:
            raise InvalidArgument("用户名或密码哈希不符合要求")
        if self.session.scalar(select(User).where(User.username == data.username)):
            raise Conflict("用户名已存在", username=data.username)
        user = User(
            username=data.username,
            display_name=data.display_name,
            password_hash=data.password_hash,
            role=data.role,
            status="active",
        )
        self.session.add(user)
        self.session.flush()
        return public_columns(user, exclude={"password_hash"})

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        user = self.session.scalar(select(User).where(User.username == username))
        return public_columns(user) if user else None

    def save_session(self, data: SessionCreate, now: datetime) -> dict[str, Any]:
        self.require_write()
        now_db, expires = utc_naive(now), utc_naive(data.expires_at)
        user = self.get_or_404(User, data.user_id, "用户")
        if user.status != "active" or expires <= now_db:
            raise InvalidArgument("会话用户无效或到期时间不正确")
        if self.session.get(SessionRecord, data.id_hash):
            raise Conflict("会话摘要已存在")
        record = SessionRecord(id_hash=data.id_hash, user_id=data.user_id, expires_at=expires)
        self.session.add(record)
        self.session.flush()
        return public_columns(record)

    def get_active_session(self, id_hash: str, now: datetime) -> dict[str, Any] | None:
        record = self.session.get(SessionRecord, id_hash)
        if record is None or record.revoked_at is not None or record.expires_at <= utc_naive(now):
            return None
        user = self.session.get(User, record.user_id)
        if user is None or user.status != "active":
            return None
        return {"session": public_columns(record), "user": public_columns(user, exclude={"password_hash"})}

    def revoke_session(self, id_hash: str, now: datetime) -> bool:
        self.require_write()
        record = self.session.get(SessionRecord, id_hash)
        if record is None or record.revoked_at is not None:
            return False
        record.revoked_at = utc_naive(now)
        self.session.flush()
        return True


class CatalogRepository(Repository):
    def create_entity(self, data: EntityCreate) -> dict[str, Any]:
        self.require_write()
        if data.type not in ALLOWED_ENTITY_TYPES:
            raise InvalidArgument("未知实体类型", type=data.type)
        if self.session.get(Entity, data.id):
            raise Conflict("实体 ID 已存在", id=data.id)
        if data.type == "camera":
            parent = self.get_or_404(Entity, data.parent_entity_id, "摄像头所属景区")
            if parent.type != "scenic":
                raise MetricEntityMismatch("摄像头必须关联景区")
        entity = Entity(**asdict(data), status="active")
        self.session.add(entity)
        self.session.flush()
        return public_columns(entity)

    def upsert_metric(self, data: MetricCreate) -> dict[str, Any]:
        self.require_write()
        if not data.supported_entity_types or not set(data.supported_entity_types) <= ALLOWED_ENTITY_TYPES:
            raise InvalidArgument("指标适用对象无效")
        metric = self.session.get(Metric, data.code)
        values = asdict(data)
        if metric is None:
            metric = Metric(**values)
            self.session.add(metric)
        else:
            for key, value in values.items():
                if key != "code":
                    setattr(metric, key, value)
        self.session.flush()
        return public_columns(metric)

    def list_entities(
        self, *, type: str | None = None, region_code: str | None = None,
        q: str | None = None, page: Page = Page(), include_cameras: bool = False,
    ) -> dict[str, Any]:
        conditions = []
        if type:
            conditions.append(Entity.type == type)
        elif not include_cameras:
            conditions.append(Entity.type != "camera")
        if region_code:
            conditions.append(Entity.region_code == region_code)
        if q:
            conditions.append(Entity.name.like(f"%{q[:50]}%"))
        total = self.session.scalar(select(func.count()).select_from(Entity).where(*conditions)) or 0
        rows = self.session.scalars(
            select(Entity).where(*conditions).order_by(Entity.name, Entity.id)
            .offset((page.page - 1) * page.page_size).limit(page.page_size)
        ).all()
        items = []
        for row in rows:
            item = public_columns(row)
            capabilities = self.session.execute(
                select(Observation.metric_code, Observation.grain).where(Observation.entity_id == row.id).distinct()
            ).all()
            item["available_metrics"] = sorted({x[0] for x in capabilities})
            item["available_grains"] = sorted({x[1] for x in capabilities})
            items.append(item)
        return {"items": items, "page": page.page, "page_size": page.page_size, "total": total}

    def list_metrics(self, entity_type: str | None = None) -> list[dict[str, Any]]:
        metrics = self.session.scalars(select(Metric).order_by(Metric.code)).all()
        return [public_columns(x) for x in metrics if not entity_type or entity_type in x.supported_entity_types]

    def get_source(self, source_id: str) -> dict[str, Any]:
        return public_columns(self.get_or_404(Source, source_id, "来源"))


class ProvenanceRepository(Repository):
    def register_source(self, data: SourceCreate) -> dict[str, Any]:
        self.require_write()
        if data.native_grain is not None and data.native_grain not in ALLOWED_GRAINS:
            raise InvalidArgument("来源粒度无效", grain=data.native_grain)
        if self.session.get(Source, data.id):
            raise Conflict("来源 ID 已存在", id=data.id)
        source = Source(**asdict(data))
        self.session.add(source)
        self.session.flush()
        return public_columns(source)

    def register_asset(self, data: AssetCreate) -> dict[str, Any]:
        self.require_write()
        self.get_or_404(Source, data.source_id, "来源")
        if len(data.sha256) != 64 or data.byte_size < 0:
            raise InvalidArgument("原件摘要或大小无效")
        existing = self.session.scalar(
            select(SourceAsset).where(SourceAsset.source_id == data.source_id, SourceAsset.sha256 == data.sha256)
        )
        if existing:
            return public_columns(existing)
        asset = SourceAsset(
            **{**asdict(data), "retrieved_at": utc_naive(data.retrieved_at),
               "published_at": utc_naive(data.published_at) if data.published_at else None}
        )
        self.session.add(asset)
        self.session.flush()
        return public_columns(asset)

    def get_asset(self, asset_id: str, *, internal: bool = False) -> dict[str, Any]:
        excluded = set() if internal else {"relative_path"}
        return public_columns(self.get_or_404(SourceAsset, asset_id, "原件"), exclude=excluded)

    def check_due_sources(self, now: datetime, interval_days: int = 7) -> list[dict[str, Any]]:
        now_db = utc_naive(now)
        threshold = now_db - timedelta(days=interval_days)
        rows = self.session.scalars(select(Source).where(
            Source.enabled.is_(True),
            or_(Source.last_checked_at.is_(None), Source.last_checked_at <= threshold),
        ).order_by(Source.last_checked_at, Source.id)).all()
        return [public_columns(row) for row in rows]


def _complete_periods(start: date, end: date, grain: str) -> tuple[list[tuple[date, date]], list[dict[str, Any]]]:
    if end < start:
        raise InvalidArgument("end 必须不早于 start")
    diagnostics: list[dict[str, Any]] = []
    periods: list[tuple[date, date]] = []
    if grain == "day":
        current = start
        while current <= end:
            periods.append((current, current))
            current += timedelta(days=1)
    elif grain == "week":
        current = start
        if current.weekday() != 0:
            next_monday = current + timedelta(days=(7 - current.weekday()))
            diagnostics.append({"code": "partial_edge_excluded", "start": start, "end": next_monday - timedelta(days=1)})
            current = next_monday
        while current + timedelta(days=6) <= end:
            periods.append((current, current + timedelta(days=6)))
            current += timedelta(days=7)
        if current <= end:
            diagnostics.append({"code": "partial_edge_excluded", "start": current, "end": end})
    elif grain == "month":
        current = start
        if current.day != 1:
            year, month = current.year + (current.month == 12), 1 if current.month == 12 else current.month + 1
            next_month = date(year, month, 1)
            diagnostics.append({"code": "partial_edge_excluded", "start": start, "end": next_month - timedelta(days=1)})
            current = next_month
        while current <= end:
            month_end = date(current.year, current.month, calendar.monthrange(current.year, current.month)[1])
            if month_end > end:
                diagnostics.append({"code": "partial_edge_excluded", "start": current, "end": end})
                break
            periods.append((current, month_end))
            year, month = current.year + (current.month == 12), 1 if current.month == 12 else current.month + 1
            current = date(year, month, 1)
    else:
        raise UnsupportedGrain("普通时序只支持 day/week/month", grain=grain)
    return periods, diagnostics


class ObservationRepository(Repository):
    def start_run(
        self, *, source_id: str, asset_id: str | None, dry_run: bool, reason: str,
        parser_version: str = "standard_csv/1", normalizer_version: str = "core/1",
        quality_rule_version: str = "core/1", job_id: str | None = None,
        attempt_id: str | None = None,
    ) -> IngestionRun:
        self.require_write()
        self.get_or_404(Source, source_id, "来源")
        if asset_id:
            asset = self.get_or_404(SourceAsset, asset_id, "原件")
            if asset.source_id != source_id:
                raise InvalidArgument("原件不属于该来源")
        run = IngestionRun(
            source_id=source_id, asset_id=asset_id, dry_run=dry_run, reason=reason,
            parser_version=parser_version, normalizer_version=normalizer_version,
            quality_rule_version=quality_rule_version, job_id=job_id, attempt_id=attempt_id,
            status="running", started_at=utcnow_naive(),
        )
        self.session.add(run)
        self.session.flush()
        return run

    def add_quality_issue(
        self, run_id: str, *, row_locator: str | None, code: str, message: str,
        severity: str = "error", raw_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.require_write()
        issue = QualityIssue(
            run_id=run_id, row_locator=row_locator, code=code, message=message,
            severity=severity, raw_payload=raw_payload, status="open",
        )
        self.session.add(issue)
        self.session.flush()
        return public_columns(issue)

    def add_or_revise(
        self, data: ObservationInput, run_id: str, *, dry_run: bool = False,
        change_reason: str = "来源重新发布了同周期数据",
    ) -> tuple[str, dict[str, Any] | None]:
        self.require_write()
        if data.grain not in ALLOWED_GRAINS or data.quality_status not in ALLOWED_QUALITY:
            raise InvalidArgument("粒度或质量状态无效")
        if data.value < 0 or data.period_end < data.period_start:
            raise InvalidArgument("数值或统计周期无效")
        entity = self.get_or_404(Entity, data.entity_id, "实体")
        metric = self.get_or_404(Metric, data.metric_code, "指标")
        if entity.type not in metric.supported_entity_types:
            raise MetricEntityMismatch("指标不适用于该实体", entity_type=entity.type, metric=data.metric_code)
        if data.unit != metric.unit:
            raise InvalidArgument("标准单位与指标字典不一致", expected=metric.unit, actual=data.unit)
        asset = self.get_or_404(SourceAsset, data.asset_id, "原件")
        run = self.get_or_404(IngestionRun, run_id, "导入批次")
        if asset.source_id != data.source_id or run.source_id != data.source_id or run.asset_id != data.asset_id:
            raise InvalidArgument("观测、来源、原件和批次不一致")
        existing = self.session.scalar(select(Observation).where(
            Observation.entity_id == data.entity_id,
            Observation.metric_code == data.metric_code,
            Observation.period_start == data.period_start,
            Observation.period_end == data.period_end,
            Observation.grain == data.grain,
            Observation.source_id == data.source_id,
        ))
        business = asdict(data)
        comparable = {
            "value", "unit", "original_value", "original_unit", "conversion_factor", "asset_id",
            "source_locator", "published_at", "quality_status", "methodology_version",
        }
        if existing:
            unchanged = all(
                (utc_aware(getattr(existing, key)) == business[key].astimezone(timezone.utc) if key == "published_at" and business[key]
                 else getattr(existing, key) == business[key])
                for key in comparable
            )
            if unchanged:
                return "skipped", public_columns(existing)
            if dry_run:
                return "revised", public_columns(existing)
            snapshot = public_columns(existing)
            snapshot["snapshot_schema_version"] = "v1"
            snapshot = {
                key: (str(value) if isinstance(value, Decimal) else value.isoformat() if isinstance(value, (date, datetime)) else value)
                for key, value in snapshot.items()
            }
            self.session.add(ObservationRevision(
                observation_id=existing.id, version=existing.row_version, snapshot=snapshot,
                superseded_by_run_id=run_id, change_reason=change_reason, changed_at=utcnow_naive(),
            ))
            for key in comparable:
                value = business[key]
                if key == "published_at" and value:
                    value = utc_naive(value)
                setattr(existing, key, value)
            existing.ingestion_run_id = run_id
            existing.ingested_at = utcnow_naive()
            existing.row_version += 1
            self.session.flush()
            return "revised", public_columns(existing)
        if dry_run:
            return "imported", None
        values = business | {
            "published_at": utc_naive(data.published_at) if data.published_at else None,
            "ingestion_run_id": run_id,
            "ingested_at": utcnow_naive(),
            "value_kind": "observed",
        }
        observation = Observation(**values)
        self.session.add(observation)
        self.session.flush()
        return "imported", public_columns(observation)

    def finish_run(self, run_id: str, *, counts: dict[str, int], status: str = "succeeded", error: str | None = None) -> dict[str, Any]:
        self.require_write()
        run = self.get_or_404(IngestionRun, run_id, "导入批次")
        run.status, run.finished_at, run.error_summary = status, utcnow_naive(), error
        for key in ("processed_count", "imported_count", "revised_count", "skipped_count", "quarantined_count"):
            setattr(run, key, int(counts.get(key.removesuffix("_count"), counts.get(key, 0))))
        self.session.flush()
        return public_columns(run)

    def query_series(self, query: SeriesQuery) -> dict[str, Any]:
        if query.quality not in {"verified", "all"}:
            raise InvalidArgument("quality 只能是 verified 或 all")
        if query.end < query.start or (query.end - query.start).days > 366 * 20:
            raise QueryLimitExceeded("查询范围无效或超过 20 年")
        entity = self.get_or_404(Entity, query.entity_id, "实体")
        metric = self.get_or_404(Metric, query.metric, "指标")
        if entity.type not in metric.supported_entity_types:
            raise MetricEntityMismatch("指标不适用于该实体")
        periods, diagnostics = _complete_periods(query.start, query.end, query.grain)
        if len(periods) > 10000:
            raise QueryLimitExceeded("查询点数超过 10000")
        rows = self.session.scalars(select(Observation).where(
            Observation.entity_id == query.entity_id,
            Observation.metric_code == query.metric,
            Observation.grain == query.grain,
            Observation.period_start >= query.start,
            Observation.period_end <= query.end,
            *( [Observation.quality_status == "verified"] if query.quality == "verified" else [] ),
        ).order_by(Observation.period_start, Observation.source_id, Observation.id)).all()
        other_grains = set(self.session.scalars(select(Observation.grain).where(
            Observation.entity_id == query.entity_id, Observation.metric_code == query.metric
        ).distinct()).all())
        policies = self.session.scalars(select(SeriesPolicy).where(
            SeriesPolicy.entity_id == query.entity_id,
            SeriesPolicy.metric_code == query.metric,
            SeriesPolicy.grain == query.grain,
            SeriesPolicy.valid_from <= query.end,
            SeriesPolicy.valid_until >= query.start,
        )).all()
        if not rows and not policies and other_grains and query.grain not in other_grains:
            raise UnsupportedGrain("该序列不支持请求粒度", supported=sorted(other_grains))
        selected: list[Observation] = []
        grouped: dict[tuple[date, date], list[Observation]] = defaultdict(list)
        for row in rows:
            grouped[(row.period_start, row.period_end)].append(row)
        if query.quality == "all":
            selected = rows
        else:
            for period, candidates in grouped.items():
                if len(candidates) == 1:
                    selected.append(candidates[0])
                    continue
                matching = [p for p in policies if p.valid_from <= period[0] and p.valid_until >= period[1]]
                if len(matching) == 1:
                    chosen = next((x for x in candidates if x.source_id == matching[0].source_id), None)
                    if chosen:
                        selected.append(chosen)
                        continue
                diagnostics.append({"code": "source_conflict", "start": period[0], "end": period[1]})
        selected_keys = {(x.period_start, x.period_end) for x in selected if x.quality_status == "verified"}
        missing = [{"period_start": a, "period_end": b} for a, b in periods if (a, b) not in selected_keys]
        points = []
        for row in selected:
            source = self.session.get(Source, row.source_id)
            asset = self.session.get(SourceAsset, row.asset_id)
            points.append({
                "id": row.id, "version": row.row_version, "metric": row.metric_code,
                "value": row.value, "unit": row.unit, "period_start": row.period_start,
                "period_end": row.period_end, "grain": row.grain, "source_ids": [row.source_id],
                "published_at": utc_aware(row.published_at), "ingested_at": utc_aware(row.ingested_at),
                "quality_status": row.quality_status,
                "provenance": {"source_id": row.source_id, "asset_id": row.asset_id,
                               "source_url": source.url if source else None,
                               "asset_url": asset.url if asset else None,
                               "source_locator": row.source_locator, "run_id": row.ingestion_run_id},
            })
        coverage = Decimal(len(periods) - len(missing)) / Decimal(len(periods)) if periods else Decimal("0")
        return {
            "entity": {"id": entity.id, "name": entity.name, "type": entity.type},
            "metric": public_columns(metric), "grain": query.grain, "points": points,
            "missing_periods": missing, "coverage_ratio": coverage,
            "diagnostics": diagnostics,
            "selection_policy_version": policies[0].policy_version if len(policies) == 1 else None,
        }


class JobRepository(Repository):
    @staticmethod
    def validate_count(value: int, name: str, *, positive: bool = False) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
            raise InvalidArgument("计数必须是正整数" if positive else "计数必须是非负整数", field=name)

    def enqueue_or_get(self, data: JobCreate, now: datetime) -> dict[str, Any]:
        self.require_write()
        existing = self.session.scalar(select(Job).where(Job.type == data.type, Job.idempotency_key == data.idempotency_key))
        if existing:
            if existing.payload != data.payload or existing.payload_version != data.payload_version:
                raise Conflict("相同幂等键对应不同任务内容")
            return public_columns(existing)
        job = Job(**asdict(data), status="queued", available_at=utc_naive(now))
        self.session.add(job)
        self.session.flush()
        return public_columns(job)

    def claim_job(
        self, worker_id: str, now: datetime, lease_seconds: int = 120, *,
        job_type: str | None = None, job_id: str | None = None,
    ) -> dict[str, Any] | None:
        """领取一个到期排队任务；筛选精确匹配，无匹配时不回退。"""
        self.require_write()
        self.validate_count(lease_seconds, "lease_seconds", positive=True)
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 100:
            raise InvalidArgument("worker_id 必须是长度不超过100的非空字符串")
        filters = []
        for name, value, column in (("job_type", job_type, Job.type), ("job_id", job_id, Job.id)):
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise InvalidArgument("任务筛选参数必须是非空字符串", field=name)
                # 普通等值保留索引候选查询；二进制复核避免 MySQL 排序规则
                # 将大小写或尾部空格不同的筛选值视为同一字符串。
                filters.extend((column == value, cast(column, LargeBinary) == value.encode("utf-8")))
        now_db = utc_naive(now)
        job = self.session.scalar(
            select(Job).where(Job.status == "queued", Job.available_at <= now_db, *filters)
            .order_by(Job.created_at, Job.id).with_for_update(skip_locked=True).limit(1)
            .execution_options(populate_existing=True)
        )
        if job is None:
            return None
        job.status, job.started_at, job.attempt_count = "running", now_db, job.attempt_count + 1
        token = str(uuid4())
        attempt = JobAttempt(
            job_id=job.id, attempt_no=job.attempt_count, worker_id=worker_id,
            lease_token=token, lease_expires_at=now_db + timedelta(seconds=lease_seconds),
            heartbeat_at=now_db, started_at=now_db,
        )
        self.session.add(attempt)
        self.session.flush()
        return {"job": public_columns(job), "lease": asdict(JobLease(job.id, job.attempt_count, token))}

    def ensure_lease(self, lease: JobLease, now: datetime | None = None) -> JobAttempt:
        # 所有租约写入先锁 Job，再锁当前尝试；事务提交/回滚前持锁。
        # populate_existing 防止同一 Session 的旧对象绕过恢复或续约结果。
        job_query = select(Job).where(Job.id == lease.job_id).execution_options(populate_existing=True)
        if self.writable:
            job_query = job_query.with_for_update()
        job = self.session.scalar(job_query)
        if not job or job.id != lease.job_id or job.status != "running" or job.attempt_count != lease.attempt_no:
            raise LeaseLost("任务租约已失效", job_id=lease.job_id)
        attempt_query = select(JobAttempt).where(
            JobAttempt.job_id == lease.job_id,
            JobAttempt.attempt_no == lease.attempt_no,
        ).execution_options(populate_existing=True)
        if self.writable:
            attempt_query = attempt_query.with_for_update()
        attempt = self.session.scalar(attempt_query)
        # 默认实时时钟在获得锁后读取，避免等待期间到期仍被接受。
        now_db = utc_naive(now) if now is not None else utcnow_naive()
        if (not attempt or attempt.lease_token != lease.lease_token or attempt.finished_at is not None
                or attempt.outcome is not None or attempt.lease_expires_at <= now_db):
            raise LeaseLost("任务租约已失效", job_id=lease.job_id)
        return attempt

    def recover_expired(
        self, now: datetime, max_attempts: int = 3, *, batch_size: int = 100,
    ) -> dict[str, int]:
        """恢复当前到期运行尝试；调用方控制事务与调度，不创建新尝试。"""
        self.require_write()
        if not isinstance(now, datetime):
            raise InvalidArgument("now 必须是带时区的时间", field="now")
        now_db = utc_naive(now)
        for name, value in (("max_attempts", max_attempts), ("batch_size", batch_size)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidArgument("恢复数量参数必须是正整数", field=name)
        # 候选读取不加锁；逐个主键锁定后复核，避免连接查询改变锁顺序。
        candidates = self.session.scalars(select(Job.id).join(JobAttempt, and_(
            JobAttempt.job_id == Job.id, JobAttempt.attempt_no == Job.attempt_count,
        )).where(
            Job.status == "running", JobAttempt.finished_at.is_(None), JobAttempt.outcome.is_(None),
            JobAttempt.lease_expires_at <= now_db,
        ).order_by(Job.id).limit(batch_size)).all()
        counts = {"requeued": 0, "failed": 0}
        for job_id in candidates:
            job = self.session.scalar(select(Job).where(Job.id == job_id)
                .with_for_update(skip_locked=True).execution_options(populate_existing=True))
            if not job or job.status != "running":
                continue
            attempt = self.session.scalar(select(JobAttempt).where(
                JobAttempt.job_id == job.id, JobAttempt.attempt_no == job.attempt_count,
            ).with_for_update(skip_locked=True).execution_options(populate_existing=True))
            if (not attempt or attempt.finished_at is not None or attempt.outcome is not None
                    or attempt.lease_expires_at > now_db):
                continue
            attempt.finished_at, attempt.outcome, attempt.error_summary = now_db, "expired", "lease_expired"
            if job.attempt_count < max_attempts:
                job.status, job.available_at = "queued", now_db
                job.started_at = job.finished_at = None
                job.processed_count = job.imported_count = 0
                job.result_refs = job.error_summary = None
                counts["requeued"] += 1
            else:
                job.status, job.finished_at, job.error_summary = "failed", now_db, "lease_expired"
                counts["failed"] += 1
        self.session.flush()
        return counts

    def heartbeat(self, lease: JobLease, now: datetime, lease_seconds: int = 120) -> datetime:
        self.require_write()
        self.validate_count(lease_seconds, "lease_seconds", positive=True)
        attempt = self.ensure_lease(lease, now)
        now_db = utc_naive(now)
        attempt.heartbeat_at = now_db
        attempt.lease_expires_at = now_db + timedelta(seconds=lease_seconds)
        self.session.flush()
        return utc_aware(attempt.lease_expires_at)  # type: ignore[return-value]

    def update_progress(self, lease: JobLease, processed_count: int, imported_count: int,
                        now: datetime | None = None) -> dict[str, Any]:
        self.require_write()
        self.validate_count(processed_count, "processed_count")
        self.validate_count(imported_count, "imported_count")
        if imported_count > processed_count:
            raise InvalidArgument("imported_count 不能超过 processed_count")
        self.ensure_lease(lease, now)
        # ensure_lease 返回 Attempt；Job 若离开 identity map，普通 get 会重新
        # 读取 MySQL 的旧事务快照。写入必须继续使用 locking current read。
        job = self.locked(Job, lease.job_id, "任务")
        if processed_count < job.processed_count or imported_count < job.imported_count:
            raise Conflict("同一尝试的进度不能倒退")
        job.processed_count, job.imported_count = processed_count, imported_count
        self.session.flush()
        return self.get_job(job.id)

    def retry_failed(self, job_id: str, reason: str, operator_id: str,
                     now: datetime) -> dict[str, Any]:
        self.require_write()
        now_db = utc_naive(now)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise InvalidArgument("重试原因必须是1到2000字符的非空字符串")
        if not isinstance(operator_id, str) or not operator_id.strip():
            raise InvalidArgument("必须提供管理员 operator_id")
        job = self.locked(Job, job_id, "任务")
        operator = self.locked(User, operator_id, "操作者")
        if operator.role != "admin" or operator.status != "active":
            raise InvalidPermission("只有有效管理员可以手动重试")
        if job.status != "failed":
            raise Conflict("只有 failed 任务可手动重试", status=job.status)
        attempt = self.session.scalar(select(JobAttempt).where(
            JobAttempt.job_id == job.id, JobAttempt.attempt_no == job.attempt_count
        ).with_for_update().execution_options(populate_existing=True))
        if attempt and (attempt.finished_at is None or attempt.outcome is None):
            raise Conflict("当前尝试尚未结束，不能重试")
        self.session.add(JobRetry(job_id=job.id, attempt_count=job.attempt_count,
            operator_id=operator_id, reason=reason.strip(), requested_at=now_db))
        job.status, job.available_at = "queued", now_db
        job.started_at = job.finished_at = None
        job.processed_count = job.imported_count = 0
        job.result_refs = job.error_summary = None
        self.session.flush()
        return self.get_job(job.id)

    def complete_job(self, lease: JobLease, result_refs: dict[str, Any], now: datetime) -> dict[str, Any]:
        self.require_write()
        attempt = self.ensure_lease(lease, now)
        job = self.locked(Job, lease.job_id, "任务")
        now_db = utc_naive(now)
        attempt.finished_at, attempt.outcome = now_db, "succeeded"
        job.status, job.finished_at, job.result_refs = "succeeded", now_db, result_refs
        self.session.flush()
        return public_columns(job)

    def fail_job(self, lease: JobLease, error_summary: str, now: datetime) -> dict[str, Any]:
        self.require_write()
        attempt = self.ensure_lease(lease, now)
        job = self.locked(Job, lease.job_id, "任务")
        now_db = utc_naive(now)
        attempt.finished_at, attempt.outcome, attempt.error_summary = now_db, "failed", error_summary[:2000]
        job.status, job.finished_at, job.error_summary = "failed", now_db, error_summary[:2000]
        self.session.flush()
        return public_columns(job)

    def get_job(self, job_id: str) -> dict[str, Any]:
        result = public_columns(self.get_or_404(Job, job_id, "任务"), exclude={"payload"})
        retries = self.session.scalars(select(JobRetry).where(JobRetry.job_id == job_id)
            .order_by(JobRetry.requested_at, JobRetry.id)).all()
        result["retry_history"] = [public_columns(row) for row in retries]
        return result


class HolidayRepository(Repository):
    def create_holiday(self, data: dict[str, Any]) -> dict[str, Any]:
        self.require_write()
        source = self.get_or_404(Source, data["definition_source_id"], "定义来源")
        asset = self.get_or_404(SourceAsset, data["definition_asset_id"], "定义原件")
        if asset.source_id != source.id:
            raise InvalidArgument("假期定义的来源与原件不一致")
        expected_days = (data["end_date"] - data["start_date"]).days + 1
        if data["days_count"] != expected_days or expected_days <= 0:
            raise InvalidArgument("假期天数与起止日期不一致")
        existing = self.session.scalar(select(Holiday).where(
            Holiday.kind == data["kind"], Holiday.year == data["year"],
            Holiday.region_code == data["region_code"], Holiday.scope_key == data["scope_key"],
        ))
        if existing:
            raise Conflict("相同范围的假期定义已存在")
        holiday = Holiday(**data)
        self.session.add(holiday)
        self.session.flush()
        return public_columns(holiday)

    def list_holidays(self, *, year: int | None = None, kind: str | None = None, region_code: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        if year is not None:
            conditions.append(Holiday.year == year)
        if kind:
            conditions.append(Holiday.kind == kind)
        if region_code:
            conditions.append(Holiday.region_code == region_code)
        rows = self.session.scalars(select(Holiday).where(*conditions).order_by(Holiday.start_date, Holiday.id)).all()
        return [public_columns(row) for row in rows]

    def get_holiday(self, holiday_id: str) -> dict[str, Any]:
        return public_columns(self.get_or_404(Holiday, holiday_id, "假期"))


class EventRepository(Repository):
    def _snapshot(self, event: Event, entity_ids: list[str]) -> dict[str, Any]:
        snapshot = public_columns(event)
        snapshot["affected_entity_ids"] = sorted(entity_ids)
        snapshot["snapshot_schema_version"] = "v1"
        return {
            key: value.isoformat() if isinstance(value, (date, datetime)) else value
            for key, value in snapshot.items()
        }

    def create_event(self, data: dict[str, Any], *, operator_id: str | None, now: datetime) -> dict[str, Any]:
        self.require_write()
        values = dict(data)
        entity_ids = list(values.pop("affected_entity_ids", []))
        source = self.get_or_404(Source, values["source_id"], "事件来源")
        asset = self.get_or_404(SourceAsset, values["asset_id"], "事件原件")
        if asset.source_id != source.id or values.get("authority") != "official":
            raise InvalidArgument("官方事件必须关联一致且可核验的来源与原件")
        for key in ("starts_at", "ends_at", "published_at", "last_verified_at"):
            if values.get(key):
                values[key] = utc_naive(values[key])
        event = Event(**values)
        self.session.add(event)
        self.session.flush()
        for entity_id in entity_ids:
            self.get_or_404(Entity, entity_id, "影响对象")
            self.session.add(EventEntity(event_id=event.id, entity_id=entity_id))
        history = EventHistory(
            event_id=event.id, version=1, snapshot=self._snapshot(event, entity_ids),
            change_note="创建事件", operator_user_id=operator_id, changed_at=utc_naive(now),
        )
        self.session.add(history)
        self.session.flush()
        return self.get_event(event.id)

    def update_event(
        self, event_id: str, patch: dict[str, Any], *, expected_version: int,
        operator_id: str | None, now: datetime,
    ) -> dict[str, Any]:
        self.require_write()
        event = self.get_or_404(Event, event_id, "事件")
        if event.row_version != expected_version:
            raise Conflict("事件版本已变化，请刷新后重试", current_version=event.row_version)
        values = dict(patch)
        change_note = values.pop("change_note", "").strip()
        if not change_note:
            raise InvalidArgument("更新事件必须填写 change_note")
        entity_ids = values.pop("affected_entity_ids", None)
        if "asset_id" in values:
            asset = self.get_or_404(SourceAsset, values["asset_id"], "事件原件")
            source_id = values.get("source_id", event.source_id)
            if asset.source_id != source_id:
                raise InvalidArgument("事件来源与原件不一致")
        allowed = {"description", "severity", "ends_at", "status", "source_id", "asset_id", "operator_note", "last_verified_at"}
        for key, value in values.items():
            if key not in allowed:
                raise InvalidArgument("不允许修改事件字段", field=key)
            if key in {"ends_at", "last_verified_at"} and value:
                value = utc_naive(value)
            setattr(event, key, value)
        if entity_ids is not None:
            self.session.query(EventEntity).filter(EventEntity.event_id == event_id).delete()
            for entity_id in entity_ids:
                self.get_or_404(Entity, entity_id, "影响对象")
                self.session.add(EventEntity(event_id=event_id, entity_id=entity_id))
        else:
            entity_ids = list(self.session.scalars(select(EventEntity.entity_id).where(EventEntity.event_id == event_id)).all())
        event.row_version += 1
        self.session.flush()
        self.session.add(EventHistory(
            event_id=event.id, version=event.row_version,
            snapshot=self._snapshot(event, list(entity_ids)), change_note=change_note,
            operator_user_id=operator_id, changed_at=utc_naive(now),
        ))
        self.session.flush()
        return self.get_event(event_id)

    def get_event(self, event_id: str) -> dict[str, Any]:
        event = self.get_or_404(Event, event_id, "事件")
        entity_ids = list(self.session.scalars(select(EventEntity.entity_id).where(EventEntity.event_id == event_id)).all())
        history = self.session.scalars(select(EventHistory).where(EventHistory.event_id == event_id).order_by(EventHistory.version)).all()
        result = public_columns(event)
        result["affected_entity_ids"] = entity_ids
        result["timeline"] = [public_columns(row) for row in history]
        return result

    def list_events(self, *, region_code: str | None = None, status: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
        conditions = []
        if region_code:
            conditions.append(Event.region_code == region_code)
        if status:
            conditions.append(Event.status == status)
        if status == "active" and now:
            current = utc_naive(now)
            conditions.extend((Event.starts_at <= current, or_(Event.ends_at.is_(None), Event.ends_at >= current)))
        rows = self.session.scalars(select(Event).where(*conditions).order_by(Event.starts_at.desc(), Event.id)).all()
        return [self.get_event(row.id) for row in rows]


class KnowledgeRepository(Repository):
    def _owned_index(self, index_id: str, lease: JobLease) -> tuple[KnowledgeDocument, KnowledgeIndex]:
        JobRepository(self.session, True).ensure_lease(lease)
        existing = self.get_or_404(KnowledgeIndex, index_id, "索引")
        if existing.job_id != lease.job_id:
            raise LeaseLost("任务与知识索引不匹配")
        document = self.locked(KnowledgeDocument, existing.document_id, "知识文档")
        index = self.locked(KnowledgeIndex, index_id, "索引")
        if index.job_id != lease.job_id:
            raise LeaseLost("任务与知识索引不匹配")
        return document, index

    def get_document(self, document_id: str) -> dict[str, Any]:
        document = self.get_or_404(KnowledgeDocument, document_id, "知识文档")
        result = public_columns(document)
        result["entity_ids"] = sorted(self.session.scalars(select(DocumentEntity.entity_id).where(
            DocumentEntity.document_id == document_id
        )).all())
        return result

    def list_documents(self, *, entity_id: str | None = None, page: Page = Page()) -> dict[str, Any]:
        conditions = []
        if entity_id is not None:
            self.get_or_404(Entity, entity_id, "实体")
            conditions.append(KnowledgeDocument.id.in_(select(DocumentEntity.document_id).where(
                DocumentEntity.entity_id == entity_id
            )))
        total = self.session.scalar(select(func.count()).select_from(KnowledgeDocument).where(*conditions)) or 0
        rows = self.session.scalars(select(KnowledgeDocument).where(*conditions).order_by(
            KnowledgeDocument.published_at.desc(), KnowledgeDocument.id
        ).offset((page.page - 1) * page.page_size).limit(page.page_size)).all()
        return {"items": [self.get_document(row.id) for row in rows],
                "page": page.page, "page_size": page.page_size, "total": total}

    def register_document(self, data: dict[str, Any], entity_ids: Iterable[str] = ()) -> dict[str, Any]:
        self.require_write()
        asset = self.get_or_404(SourceAsset, data["asset_id"], "原件")
        if asset.source_id != data["source_id"]:
            raise InvalidArgument("文档原件不属于来源")
        values = dict(data)
        for key in ("published_at", "valid_from", "valid_until"):
            if values.get(key):
                values[key] = utc_naive(values[key])
        document = KnowledgeDocument(**values, index_status="pending")
        self.session.add(document)
        self.session.flush()
        for entity_id in entity_ids:
            self.get_or_404(Entity, entity_id, "实体")
            self.session.add(DocumentEntity(document_id=document.id, entity_id=entity_id))
        self.session.flush()
        return public_columns(document)

    def begin_index(
        self, *, document_id: str, version: str, job_id: str, parser_version: str,
        embedding_model_version: str, lease: JobLease,
    ) -> dict[str, Any]:
        self.require_write()
        JobRepository(self.session, True).ensure_lease(lease)
        if job_id != lease.job_id:
            raise LeaseLost("任务与知识索引不匹配")
        document = self.locked(KnowledgeDocument, document_id, "知识文档")
        existing = self.session.scalar(select(KnowledgeIndex).where(
            KnowledgeIndex.document_id == document_id, KnowledgeIndex.version == version
        ).with_for_update().execution_options(populate_existing=True))
        if existing:
            if existing.job_id != job_id:
                raise LeaseLost("索引版本属于其他任务")
            if existing.parser_version != parser_version or existing.embedding_model_version != embedding_model_version:
                raise Conflict("重试不能更改索引解析或模型版本")
            if existing.status not in {"building", "failed"}:
                raise Conflict("已发布索引不能重新构建")
            existing.status, existing.error_summary = "building", None
            if document.active_index_version is None:
                document.index_status = "building"
            self.session.flush()
            return public_columns(existing)
        index = KnowledgeIndex(
            document_id=document_id, version=version, job_id=job_id, status="building",
            parser_version=parser_version, embedding_model_version=embedding_model_version,
            chunk_count=0,
        )
        self.session.add(index)
        if document.active_index_version is None:
            document.index_status = "building"
        self.session.flush()
        return public_columns(index)

    def write_chunks(self, index_id: str, chunks: list[dict[str, Any]], lease: JobLease) -> dict[str, int]:
        self.require_write()
        _, index = self._owned_index(index_id, lease)
        if index.status != "building" or len(chunks) > 1000:
            raise Conflict("索引状态不允许写入或单批片段过多")
        inserted = skipped = 0
        for data in chunks:
            text_hash = hashlib.sha256(data["text"].encode("utf-8")).hexdigest()
            existing = self.session.scalar(select(KnowledgeChunk).where(
                KnowledgeChunk.index_id == index_id, KnowledgeChunk.chunk_no == data["chunk_no"]
            ).with_for_update().execution_options(populate_existing=True))
            if existing:
                if existing.text_sha256 != text_hash or existing.locator != data["locator"]:
                    raise Conflict("同一片段序号内容不一致", chunk_no=data["chunk_no"])
                skipped += 1
            else:
                self.session.add(KnowledgeChunk(
                    index_id=index_id, chunk_no=data["chunk_no"], text=data["text"],
                    locator=data["locator"], text_sha256=text_hash,
                ))
                inserted += 1
        self.session.flush()
        return {"inserted": inserted, "skipped": skipped}

    def activate_index(
        self, *, document_id: str, version: str, artifact_path: str,
        artifact_sha256: str, expected_chunk_count: int, lease: JobLease,
    ) -> dict[str, Any]:
        self.require_write()
        JobRepository(self.session, True).ensure_lease(lease)
        index = self.session.scalar(select(KnowledgeIndex).where(
            KnowledgeIndex.document_id == document_id, KnowledgeIndex.version == version
        ))
        if index is None:
            raise NotFound("索引版本不存在")
        document, index = self._owned_index(index.id, lease)
        if index.status != "building":
            raise Conflict("只有 building 索引可以发布")
        JobRepository.validate_count(expected_chunk_count, "expected_chunk_count")
        actual = len(self.session.scalars(select(KnowledgeChunk.id).where(
            KnowledgeChunk.index_id == index.id).with_for_update()).all())
        if actual != expected_chunk_count:
            raise Conflict("片段数量与预期不一致", actual=actual, expected=expected_chunk_count)
        index.status, index.chunk_count = "ready", actual
        index.artifact_path, index.artifact_sha256 = artifact_path, artifact_sha256
        document.active_index_version, document.index_status = version, "ready"
        document.row_version += 1
        self.session.flush()
        return public_columns(document)

    def fail_index(self, index_id: str, error_summary: str, lease: JobLease) -> dict[str, Any]:
        self.require_write()
        if not isinstance(error_summary, str) or not error_summary.strip():
            raise InvalidArgument("必须提供索引错误摘要")
        document, index = self._owned_index(index_id, lease)
        if index.status not in {"building", "failed"}:
            raise Conflict("已发布索引不能标为失败")
        index.status, index.error_summary = "failed", error_summary[:2000]
        if document.active_index_version is None:
            document.index_status = "failed"
        self.session.flush()
        return public_columns(index)

    def get_chunks(self, document_id: str, version: str | None = None) -> dict[str, Any]:
        document = self.get_or_404(KnowledgeDocument, document_id, "知识文档")
        target = version or document.active_index_version
        if not target:
            return {"status": "index_not_ready", "items": []}
        index = self.session.scalar(select(KnowledgeIndex).where(
            KnowledgeIndex.document_id == document_id, KnowledgeIndex.version == target,
            KnowledgeIndex.status == "ready",
        ))
        if not index:
            return {"status": "index_not_ready", "items": []}
        rows = self.session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.index_id == index.id).order_by(KnowledgeChunk.chunk_no)).all()
        return {"status": "ready", "version": target, "items": [public_columns(x) for x in rows]}


class VideoRepository(Repository):
    def _permission(self, video: Video, use: str, now: datetime | None = None,
                    *, lock: bool = False) -> PermissionRecord:
        permission = (self.locked(PermissionRecord, video.permission_record_id, "授权") if lock
                      else self.get_or_404(PermissionRecord, video.permission_record_id, "授权"))
        current = utc_naive(now) if now is not None else utcnow_naive()
        if (permission.source_id != video.source_id or permission.status != "approved"
                or use not in permission.allowed_uses or permission.valid_from > current
                or (permission.valid_until is not None and permission.valid_until <= current)
                or (use == "replay" and not permission.playback_allowed)):
            raise InvalidPermission("录像授权已失效或不允许该用途", use=use)
        return permission

    def _owned_analysis(self, analysis_id: str, lease: JobLease,
                        now: datetime | None = None) -> tuple[VideoAnalysis, Video]:
        JobRepository(self.session, True).ensure_lease(lease, now)
        analysis = self.get_or_404(VideoAnalysis, analysis_id, "录像分析")
        if analysis.job_id != lease.job_id:
            raise LeaseLost("任务与录像分析不匹配")
        video = self.locked(Video, analysis.video_id, "录像")
        analysis = self.locked(VideoAnalysis, analysis_id, "录像分析")
        if analysis.job_id != lease.job_id:
            raise LeaseLost("任务与录像分析不匹配")
        return analysis, video

    def get_permission(self, permission_id: str) -> dict[str, Any]:
        return public_columns(self.get_or_404(PermissionRecord, permission_id, "授权"), exclude={"evidence_path"})

    def get_video(self, video_id: str) -> dict[str, Any]:
        video = self.get_or_404(Video, video_id, "录像")
        result = public_columns(video)
        analyses = self.session.scalars(select(VideoAnalysis).where(VideoAnalysis.video_id == video.id)
            .order_by(VideoAnalysis.created_at, VideoAnalysis.id)).all()
        result["analyses"] = [public_columns(row) for row in analyses]
        return result

    def list_videos(self, *, scenic_id: str | None = None, camera_id: str | None = None,
                    status: str | None = None, page: Page = Page()) -> dict[str, Any]:
        conditions = []
        for name, value, column, expected_type in (
            ("scenic_id", scenic_id, Video.scenic_id, "scenic"),
            ("camera_id", camera_id, Video.camera_id, "camera"),
        ):
            if value is not None:
                entity = self.get_or_404(Entity, value, name)
                if entity.type != expected_type:
                    raise InvalidArgument("录像筛选实体类型错误", field=name)
                conditions.append(column == value)
        if status is not None:
            if status not in {"uploaded", "processing", "ready", "failed"}:
                raise InvalidArgument("录像 status 无效")
            conditions.append(Video.status == status)
        total = self.session.scalar(select(func.count()).select_from(Video).where(*conditions)) or 0
        rows = self.session.scalars(select(Video).where(*conditions)
            .order_by(Video.recorded_start_at.desc(), Video.id)
            .offset((page.page - 1) * page.page_size).limit(page.page_size)).all()
        return {"items": [self.get_video(row.id) for row in rows], "page": page.page,
                "page_size": page.page_size, "total": total}

    def get_playback_ref(self, video_id: str, now: datetime) -> dict[str, Any]:
        """仅给 D 内部文件响应使用；D 在每次请求时核验用户权限。"""
        video = self.get_or_404(Video, video_id, "录像")
        self._permission(video, "replay", now)
        asset = self.get_or_404(SourceAsset, video.asset_id, "原件")
        if self.data_root is None:
            raise InvalidArgument("回放必须配置 DATA_ROOT")
        root = self.data_root.resolve()
        relative = Path(asset.relative_path)
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root):
            raise InvalidArgument("回放路径超出 DATA_ROOT")
        if not path.is_file():
            raise NotFound("录像文件未在 DATA_ROOT 留存")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != asset.sha256 or path.stat().st_size != asset.byte_size:
            raise Conflict("回放文件与登记摘要或大小不一致")
        return {"video_id": video.id, "asset_id": asset.id, "path": str(path),
                "sha256": asset.sha256, "byte_size": asset.byte_size, "media_type": asset.media_type}

    def begin_analysis(self, analysis_id: str, lease: JobLease,
                       now: datetime | None = None) -> dict[str, Any]:
        self.require_write()
        analysis, video = self._owned_analysis(analysis_id, lease, now)
        self._permission(video, "analysis", now, lock=True)
        if analysis.status not in {"queued", "running", "failed"}:
            raise Conflict("已完成分析不能重新开始")
        analysis.status, analysis.error_summary = "running", None
        if video.status != "ready":
            video.status = "processing"
        self.session.flush()
        return public_columns(analysis)

    def fail_analysis(self, analysis_id: str, error_summary: str,
                      lease: JobLease) -> dict[str, Any]:
        self.require_write()
        if not isinstance(error_summary, str) or not error_summary.strip():
            raise InvalidArgument("必须提供分析错误摘要")
        analysis, video = self._owned_analysis(analysis_id, lease)
        if analysis.status not in {"queued", "running", "failed"}:
            raise Conflict("已完成分析不能标为失败")
        analysis.status, analysis.error_summary = "failed", error_summary[:2000]
        ready = self.session.scalar(select(VideoAnalysis.id).where(
            VideoAnalysis.video_id == video.id, VideoAnalysis.status == "succeeded").with_for_update())
        if not ready:
            video.status = "failed"
        self.session.flush()
        return public_columns(analysis)

    def register_permission(self, data: dict[str, Any]) -> dict[str, Any]:
        self.require_write()
        self.get_or_404(Source, data["source_id"], "来源")
        values = dict(data)
        values["valid_from"] = utc_naive(values["valid_from"])
        if values.get("valid_until"):
            values["valid_until"] = utc_naive(values["valid_until"])
        record = PermissionRecord(**values)
        self.session.add(record)
        self.session.flush()
        return public_columns(record, exclude={"evidence_path"})

    def register_video(self, data: dict[str, Any], now: datetime) -> dict[str, Any]:
        self.require_write()
        scenic = self.get_or_404(Entity, data["scenic_id"], "景区")
        camera = self.get_or_404(Entity, data["camera_id"], "摄像头")
        asset = self.get_or_404(SourceAsset, data["asset_id"], "原件")
        permission = self.get_or_404(PermissionRecord, data["permission_record_id"], "授权")
        now_db = utc_naive(now)
        if scenic.type != "scenic" or camera.type != "camera" or camera.parent_entity_id != scenic.id:
            raise InvalidArgument("景区与摄像头关系无效")
        if asset.source_id != data["source_id"] or permission.source_id != data["source_id"]:
            raise InvalidArgument("录像原件或授权与来源不一致")
        if permission.status != "approved" or "analysis" not in permission.allowed_uses or permission.valid_from > now_db or (permission.valid_until and permission.valid_until <= now_db):
            raise InvalidPermission("录像授权无效或不允许分析")
        values = dict(data)
        values["recorded_start_at"] = utc_naive(values["recorded_start_at"])
        values["recorded_end_at"] = utc_naive(values["recorded_end_at"])
        video = Video(**values, status="uploaded")
        self.session.add(video)
        self.session.flush()
        return public_columns(video)

    def create_analysis(self, video_id: str, job_id: str, config: dict[str, Any]) -> dict[str, Any]:
        self.require_write()
        job = self.locked(Job, job_id, "任务")
        if job.status not in {"queued", "running"}:
            raise Conflict("任务状态不允许创建分析")
        self.locked(Video, video_id, "录像")
        normalized = dict(config)
        config_hash = sha256_json(normalized)
        existing = self.session.scalar(select(VideoAnalysis).where(
            VideoAnalysis.video_id == video_id, VideoAnalysis.config_sha256 == config_hash
        ).with_for_update().execution_options(populate_existing=True))
        if existing:
            if existing.job_id != job_id:
                raise Conflict("相同配置的分析属于其他任务", analysis_id=existing.id)
            return public_columns(existing)
        analysis = VideoAnalysis(
            video_id=video_id, job_id=job_id, config_sha256=config_hash, status="queued", **normalized
        )
        self.session.add(analysis)
        self.session.flush()
        return public_columns(analysis)

    def write_counts(self, analysis_id: str, points: list[CountInput], lease: JobLease) -> dict[str, int]:
        self.require_write()
        analysis, video = self._owned_analysis(analysis_id, lease)
        self._permission(video, "analysis", lock=True)
        if len(points) > 1000:
            raise InvalidArgument("单批计数最多1000个窗口")
        if analysis.status not in {"queued", "running"}:
            raise Conflict("分析状态不允许写入；失败分析需先 begin_analysis")
        inserted = skipped = 0
        for point in points:
            JobRepository.validate_count(point.entries, "entries")
            JobRepository.validate_count(point.exits, "exits")
            start, end = utc_naive(point.window_start), utc_naive(point.window_end)
            if start < video.recorded_start_at or end > video.recorded_end_at or end <= start or point.entries < 0 or point.exits < 0:
                raise InvalidArgument("计数窗口或数值无效")
            overlap = self.session.scalar(select(CameraCount).where(
                CameraCount.analysis_id == analysis_id,
                CameraCount.window_start < end, CameraCount.window_end > start,
            ).with_for_update().execution_options(populate_existing=True))
            if overlap:
                if overlap.window_start == start and overlap.window_end == end and overlap.entries == point.entries and overlap.exits == point.exits and overlap.quality_status == point.quality_status:
                    skipped += 1
                    continue
                raise Conflict("计数窗口重叠或同窗结果冲突")
            self.session.add(CameraCount(
                analysis_id=analysis_id, window_start=start, window_end=end,
                entries=point.entries, exits=point.exits, quality_status=point.quality_status,
            ))
            inserted += 1
        analysis.status = "running"
        if video.status != "ready":
            video.status = "processing"
        self.session.flush()
        return {"inserted": inserted, "skipped": skipped}

    def complete_analysis(self, analysis_id: str, expected_window_count: int, lease: JobLease, manual_validation: dict[str, Any] | None = None) -> dict[str, Any]:
        self.require_write()
        JobRepository.validate_count(expected_window_count, "expected_window_count")
        analysis, video = self._owned_analysis(analysis_id, lease)
        self._permission(video, "analysis", lock=True)
        if analysis.status not in {"queued", "running"}:
            raise Conflict("只有未结束分析可以完成")
        actual = len(self.session.scalars(select(CameraCount.id).where(
            CameraCount.analysis_id == analysis_id).with_for_update()).all())
        if actual != expected_window_count:
            raise Conflict("计数窗口数量不符合预期", actual=actual, expected=expected_window_count)
        analysis.status, analysis.manual_validation = "succeeded", manual_validation
        if video:
            video.status = "ready"
        self.session.flush()
        return public_columns(analysis)

    def write_manual_annotations(self, annotations: list[dict[str, Any]], *,
                                 now: datetime | None = None) -> dict[str, int]:
        self.require_write()
        if not isinstance(annotations, list) or len(annotations) > 1000:
            raise InvalidArgument("单批人工标注必须是最多1000条的列表")
        current = utc_aware(utc_naive(now)) if now is not None else datetime.now(timezone.utc)
        fields = {"video_id", "camera_id", "sample_key", "version", "window_start", "window_end",
                  "entries", "exits", "annotator", "direction_note", "review_status"}
        normalized = []
        for item in annotations:
            if not isinstance(item, dict) or set(item) != fields:
                raise InvalidArgument("人工标注字段缺失或包含未知字段")
            values = dict(item)
            for key, limit in (("video_id",36),("camera_id",36),("sample_key",100),("annotator",100),("direction_note",2000)):
                if not isinstance(values[key], str) or not values[key].strip() or len(values[key]) > limit:
                    raise InvalidArgument("人工标注字符串字段无效", field=key)
            for key in ("entries", "exits", "version"):
                JobRepository.validate_count(values[key], key, positive=(key=="version"))
            if not isinstance(values["review_status"],str) or values["review_status"] not in {"pending_review", "verified", "rejected"}:
                raise InvalidArgument("人工标注review_status无效")
            values["window_start"], values["window_end"] = utc_naive(values["window_start"]), utc_naive(values["window_end"])
            if values["window_end"] <= values["window_start"]:
                raise InvalidArgument("人工标注时间窗顺序无效")
            normalized.append(values)
        inserted = skipped = 0
        # 先按视频ID锁定，避免多录像标注批次逆序加锁。
        videos = {key:self.locked(Video,key,"录像") for key in sorted({x["video_id"] for x in normalized})}
        for video in videos.values():
            self._permission(video,"analysis",current,lock=True)
        for item in normalized:
            video = videos[item["video_id"]]
            if (item["camera_id"] != video.camera_id or item["window_start"] < video.recorded_start_at
                    or item["window_end"] > video.recorded_end_at):
                raise InvalidArgument("人工标注摄像头或时间范围与录像不符")
        # 冲突时只回滚本批标注，调用方之前的独立修改仍由外层事务控制。
        with self.session.begin_nested():
            for item in normalized:
                existing = self.session.scalar(select(ManualAnnotation).where(
                    ManualAnnotation.video_id == item["video_id"], ManualAnnotation.sample_key == item["sample_key"],
                    ManualAnnotation.version == item["version"]
                ).with_for_update().execution_options(populate_existing=True))
                if existing:
                    if any(getattr(existing,key) != value for key,value in item.items()):
                        raise Conflict("同版本标注内容不一致；修订必须新版本")
                    skipped += 1
                    continue
                latest_row = self.session.scalar(select(ManualAnnotation).where(
                    ManualAnnotation.video_id == item["video_id"], ManualAnnotation.sample_key == item["sample_key"]
                ).order_by(ManualAnnotation.version.desc()).with_for_update().limit(1)
                    .execution_options(populate_existing=True))
                if latest_row is not None and latest_row.sample_key != item["sample_key"]:
                    raise Conflict("样本键与已有记录的大小写或尾部空格不同")
                latest = latest_row.version if latest_row else 0
                if item["version"] != latest+1:
                    raise Conflict("人工标注版本必须从1开始连续递增", latest_version=latest)
                self.session.add(ManualAnnotation(**item))
                self.session.flush()
                inserted += 1
        return {"inserted":inserted,"skipped":skipped}

    def list_manual_annotations(self, video_id: str, *, review_status: str | None = None,
                                latest_only: bool = True, page: Page = Page()) -> dict[str, Any]:
        self.get_or_404(Video,video_id,"录像")
        if not isinstance(latest_only,bool):
            raise InvalidArgument("latest_only必须是bool")
        conditions = [ManualAnnotation.video_id == video_id]
        if review_status is not None:
            if not isinstance(review_status,str) or review_status not in {"pending_review","verified","rejected"}:
                raise InvalidArgument("review_status无效")
            conditions.append(ManualAnnotation.review_status == review_status)
        if latest_only:
            latest = select(ManualAnnotation.sample_key,func.max(ManualAnnotation.version).label("version")).where(
                ManualAnnotation.video_id == video_id).group_by(ManualAnnotation.sample_key).subquery()
            conditions.append(tuple_(ManualAnnotation.sample_key,ManualAnnotation.version).in_(select(latest.c.sample_key,latest.c.version)))
        total = self.session.scalar(select(func.count()).select_from(ManualAnnotation).where(*conditions)) or 0
        rows = self.session.scalars(select(ManualAnnotation).where(*conditions)
            .order_by(ManualAnnotation.window_start,ManualAnnotation.sample_key,ManualAnnotation.version.desc())
            .offset((page.page-1)*page.page_size).limit(page.page_size)).all()
        return {"items":[public_columns(row) for row in rows],"page":page.page,"page_size":page.page_size,"total":total}

    def query_counts(self, video_id: str, analysis_id: str, *, start: datetime | None = None,
                     end: datetime | None = None, interval_minutes: int | None = None) -> dict[str, Any]:
        analysis = self.get_or_404(VideoAnalysis, analysis_id, "录像分析")
        if analysis.video_id != video_id:
            raise NotFound("该录像没有指定分析")
        if analysis.status != "succeeded":
            raise AnalysisNotReady("录像分析尚未完成")
        video = self.get_or_404(Video,video_id,"录像")
        left = utc_naive(start) if start is not None else video.recorded_start_at
        right = utc_naive(end) if end is not None else video.recorded_end_at
        if right <= left and start is not None and end is not None:
            raise InvalidArgument("计数查询end必须晚于start")
        native = timedelta(minutes=analysis.interval_minutes)
        if interval_minutes is not None:
            JobRepository.validate_count(interval_minutes,"interval_minutes",positive=True)
            if interval_minutes > 1440 or interval_minutes % analysis.interval_minutes:
                raise InvalidArgument("聚合间隔必须是原始间隔的整数倍")
        filtered = start is not None or end is not None or interval_minutes is not None
        left,right = max(left,video.recorded_start_at),min(right,video.recorded_end_at)
        if filtered and (right-left)/native > 20000:
            raise QueryLimitExceeded("单次计数查询最多覆盖20000个原始时间窗")
        rows = self.session.scalars(select(CameraCount).where(CameraCount.analysis_id == analysis_id,
            CameraCount.window_start >= left, CameraCount.window_end <= right)
            .order_by(CameraCount.window_start,CameraCount.id)).all() if right > left else []
        result = {"analysis": public_columns(analysis), "points": [public_columns(x) for x in rows]}
        if interval_minutes is None:
            return result
        step = timedelta(minutes=interval_minutes)
        # 对齐录像起点；不拆分输入窗，不将不足一个聚合窗的边界计为完整。
        offset = max(timedelta(),left-video.recorded_start_at)
        index = offset // step + bool(offset % step)
        cursor = video.recorded_start_at + index*step
        first_window_start = cursor
        points,missing = [],[]
        native_points = {(row.window_start,row.window_end):row for row in rows}
        expected = interval_minutes//analysis.interval_minutes
        while cursor+step <= right:
            windows = [native_points.get((cursor+i*native,cursor+(i+1)*native)) for i in range(expected)]
            if any(row is None for row in windows):
                missing.append({"window_start":utc_aware(cursor),"window_end":utc_aware(cursor+step),
                    "expected_windows":expected,"available_windows":sum(row is not None for row in windows)})
            else:
                statuses = {row.quality_status for row in windows}
                quality = "verified" if statuses == {"verified"} else ("rejected" if "rejected" in statuses else "pending_review")
                points.append({"window_start":utc_aware(cursor),"window_end":utc_aware(cursor+step),
                    "entries":sum(row.entries for row in windows),"exits":sum(row.exits for row in windows),
                    "quality_status":quality,"source_window_count":expected})
            cursor += step
        result["points"] = points
        result["aggregation"] = {"interval_minutes":interval_minutes,"native_interval_minutes":analysis.interval_minutes,
            "alignment":"recorded_start_at","missing_windows":missing,
            "incomplete_boundary_omitted":right > left and (first_window_start > left or cursor < right),
            "source_window_count":len(rows),"complete_window_count":len(points),"expected_window_count":len(points)+len(missing),
            "ignored_non_native_windows":sum((row.window_end-row.window_start != native
                or (row.window_start-video.recorded_start_at)%native != timedelta()) for row in rows)}
        return result


class ForecastRepository(Repository):
    def get_history_forecast(self, run_id: str) -> dict[str, Any]:
        run = self.get_or_404(ForecastRun,run_id,"历史预测运行")
        rows = self.session.scalars(select(ForecastPoint).where(ForecastPoint.run_id==run_id)
            .order_by(ForecastPoint.period_start,ForecastPoint.period_end,ForecastPoint.id)).all()
        return {"run":public_columns(run),"points":[public_columns(row) for row in rows]}

    def get_short_forecast(self, run_id: str) -> dict[str, Any]:
        run = self.get_or_404(ShortForecastRun,run_id,"短时预测运行")
        rows = self.session.scalars(select(ShortForecastPoint).where(ShortForecastPoint.run_id==run_id)
            .order_by(ShortForecastPoint.window_start,ShortForecastPoint.window_end,ShortForecastPoint.id)).all()
        return {"run":public_columns(run),"points":[public_columns(row) for row in rows]}

    def save_history_forecast(self, data: dict[str, Any], points: list[dict[str, Any]]) -> dict[str, Any]:
        self.require_write()
        status, horizon = data["status"], data["horizon"]
        if status == "ready" and len(points) != horizon:
            raise InvalidArgument("ready 预测点数必须等于 horizon")
        if status == "data_insufficient" and (points or not data.get("reason")):
            raise InvalidArgument("数据不足时必须无预测点并提供原因")
        run = ForecastRun(**data)
        self.session.add(run)
        self.session.flush()
        for point in points:
            self.session.add(ForecastPoint(run_id=run.id, **point))
        self.session.flush()
        return {"run": public_columns(run), "points": points}

    def save_short_forecast(self, data: dict[str, Any], points: list[dict[str, Any]]) -> dict[str, Any]:
        self.require_write()
        analysis = self.get_or_404(VideoAnalysis, data["analysis_id"], "录像分析")
        if analysis.status != "succeeded":
            raise AnalysisNotReady("录像分析尚未完成")
        expected = data["horizon_minutes"] // data["step_minutes"]
        if data["status"] == "ready" and len(points) != expected:
            raise InvalidArgument("短时预测点数与时长不匹配")
        if data["status"] != "ready" and (points or not data.get("reason")):
            raise InvalidArgument("预测不足状态必须无结果点并说明原因")
        cutoff = utc_naive(data["cutoff_at"])
        for item in data["input_manifest"].get("counts", []):
            end = datetime.fromisoformat(item["window_end"])
            if utc_naive(end) > cutoff:
                raise InvalidArgument("短时预测输入越过 cutoff_at")
        values = dict(data)
        values["cutoff_at"] = cutoff
        run = ShortForecastRun(**values)
        self.session.add(run)
        self.session.flush()
        for point in points:
            values = dict(point)
            values["window_start"] = utc_naive(values["window_start"])
            values["window_end"] = utc_naive(values["window_end"])
            self.session.add(ShortForecastPoint(run_id=run.id, **values))
        self.session.flush()
        return {"run": public_columns(run), "points": points}


class UnitOfWork:
    def __init__(self, session: Session, writable: bool, data_root: Path | None = None) -> None:
        self.session = session
        self.auth = AuthRepository(session, writable)
        self.catalog = CatalogRepository(session, writable)
        self.provenance = ProvenanceRepository(session, writable)
        self.observations = ObservationRepository(session, writable)
        self.jobs = JobRepository(session, writable)
        self.holidays = HolidayRepository(session, writable)
        self.events = EventRepository(session, writable)
        self.knowledge = KnowledgeRepository(session, writable, data_root)
        self.video = VideoRepository(session, writable, data_root)
        self.forecasts = ForecastRepository(session, writable)
