from __future__ import annotations

import calendar
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
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
    if value.tzinfo is None or value.utcoffset() is None:
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
    def __init__(self, session: Session, writable: bool) -> None:
        self.session = session
        self.writable = writable

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
            points.append({
                "id": row.id, "version": row.row_version, "metric": row.metric_code,
                "value": row.value, "unit": row.unit, "period_start": row.period_start,
                "period_end": row.period_end, "grain": row.grain, "source_ids": [row.source_id],
                "published_at": utc_aware(row.published_at), "ingested_at": utc_aware(row.ingested_at),
                "quality_status": row.quality_status,
                "provenance": {"source_id": row.source_id, "asset_id": row.asset_id,
                               "source_url": source.url if source else None,
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

    def claim_job(self, worker_id: str, now: datetime, lease_seconds: int = 120) -> dict[str, Any] | None:
        self.require_write()
        now_db = utc_naive(now)
        job = self.session.scalar(
            select(Job).where(Job.status == "queued", Job.available_at <= now_db)
            .order_by(Job.created_at, Job.id).with_for_update(skip_locked=True).limit(1)
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
        attempt = self.session.scalar(select(JobAttempt).where(
            JobAttempt.job_id == lease.job_id,
            JobAttempt.attempt_no == lease.attempt_no,
            JobAttempt.lease_token == lease.lease_token,
        ))
        job = self.session.get(Job, lease.job_id)
        now_db = utc_naive(now) if now else utcnow_naive()
        if not attempt or not job or job.status != "running" or attempt.finished_at is not None or attempt.lease_expires_at <= now_db:
            raise LeaseLost("任务租约已失效", job_id=lease.job_id)
        return attempt

    def heartbeat(self, lease: JobLease, now: datetime, lease_seconds: int = 120) -> datetime:
        self.require_write()
        attempt = self.ensure_lease(lease, now)
        now_db = utc_naive(now)
        attempt.heartbeat_at = now_db
        attempt.lease_expires_at = now_db + timedelta(seconds=lease_seconds)
        self.session.flush()
        return utc_aware(attempt.lease_expires_at)  # type: ignore[return-value]

    def complete_job(self, lease: JobLease, result_refs: dict[str, Any], now: datetime) -> dict[str, Any]:
        self.require_write()
        attempt = self.ensure_lease(lease, now)
        job = self.get_or_404(Job, lease.job_id, "任务")
        now_db = utc_naive(now)
        attempt.finished_at, attempt.outcome = now_db, "succeeded"
        job.status, job.finished_at, job.result_refs = "succeeded", now_db, result_refs
        self.session.flush()
        return public_columns(job)

    def fail_job(self, lease: JobLease, error_summary: str, now: datetime) -> dict[str, Any]:
        self.require_write()
        attempt = self.ensure_lease(lease, now)
        job = self.get_or_404(Job, lease.job_id, "任务")
        now_db = utc_naive(now)
        attempt.finished_at, attempt.outcome, attempt.error_summary = now_db, "failed", error_summary[:2000]
        job.status, job.finished_at, job.error_summary = "failed", now_db, error_summary[:2000]
        self.session.flush()
        return public_columns(job)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return public_columns(self.get_or_404(Job, job_id, "任务"), exclude={"payload"})


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
        self.get_or_404(KnowledgeDocument, document_id, "知识文档")
        existing = self.session.scalar(select(KnowledgeIndex).where(
            KnowledgeIndex.document_id == document_id, KnowledgeIndex.version == version
        ))
        if existing:
            return public_columns(existing)
        index = KnowledgeIndex(
            document_id=document_id, version=version, job_id=job_id, status="building",
            parser_version=parser_version, embedding_model_version=embedding_model_version,
            chunk_count=0,
        )
        self.session.add(index)
        self.session.flush()
        return public_columns(index)

    def write_chunks(self, index_id: str, chunks: list[dict[str, Any]], lease: JobLease) -> dict[str, int]:
        self.require_write()
        JobRepository(self.session, True).ensure_lease(lease)
        index = self.get_or_404(KnowledgeIndex, index_id, "索引")
        if index.status != "building" or len(chunks) > 1000:
            raise Conflict("索引状态不允许写入或单批片段过多")
        inserted = skipped = 0
        for data in chunks:
            text_hash = hashlib.sha256(data["text"].encode("utf-8")).hexdigest()
            existing = self.session.scalar(select(KnowledgeChunk).where(
                KnowledgeChunk.index_id == index_id, KnowledgeChunk.chunk_no == data["chunk_no"]
            ))
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
        document = self.get_or_404(KnowledgeDocument, document_id, "知识文档")
        index = self.session.scalar(select(KnowledgeIndex).where(
            KnowledgeIndex.document_id == document_id, KnowledgeIndex.version == version
        ))
        if index is None:
            raise NotFound("索引版本不存在")
        actual = self.session.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.index_id == index.id)) or 0
        if actual != expected_chunk_count:
            raise Conflict("片段数量与预期不一致", actual=actual, expected=expected_chunk_count)
        index.status, index.chunk_count = "ready", actual
        index.artifact_path, index.artifact_sha256 = artifact_path, artifact_sha256
        document.active_index_version, document.index_status = version, "ready"
        document.row_version += 1
        self.session.flush()
        return public_columns(document)

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
        self.get_or_404(Video, video_id, "录像")
        normalized = dict(config)
        config_hash = sha256_json(normalized)
        existing = self.session.scalar(select(VideoAnalysis).where(
            VideoAnalysis.video_id == video_id, VideoAnalysis.config_sha256 == config_hash
        ))
        if existing:
            return public_columns(existing)
        analysis = VideoAnalysis(
            video_id=video_id, job_id=job_id, config_sha256=config_hash, status="queued", **normalized
        )
        self.session.add(analysis)
        self.session.flush()
        return public_columns(analysis)

    def write_counts(self, analysis_id: str, points: list[CountInput], lease: JobLease) -> dict[str, int]:
        self.require_write()
        JobRepository(self.session, True).ensure_lease(lease)
        analysis = self.get_or_404(VideoAnalysis, analysis_id, "录像分析")
        video = self.get_or_404(Video, analysis.video_id, "录像")
        if analysis.job_id != lease.job_id or len(points) > 1000:
            raise LeaseLost("任务与分析不匹配或单批过大")
        inserted = skipped = 0
        for point in points:
            start, end = utc_naive(point.window_start), utc_naive(point.window_end)
            if start < video.recorded_start_at or end > video.recorded_end_at or end <= start or point.entries < 0 or point.exits < 0:
                raise InvalidArgument("计数窗口或数值无效")
            overlap = self.session.scalar(select(CameraCount).where(
                CameraCount.analysis_id == analysis_id,
                CameraCount.window_start < end, CameraCount.window_end > start,
            ))
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
        self.session.flush()
        return {"inserted": inserted, "skipped": skipped}

    def complete_analysis(self, analysis_id: str, expected_window_count: int, lease: JobLease, manual_validation: dict[str, Any] | None = None) -> dict[str, Any]:
        self.require_write()
        JobRepository(self.session, True).ensure_lease(lease)
        analysis = self.get_or_404(VideoAnalysis, analysis_id, "录像分析")
        actual = self.session.scalar(select(func.count()).select_from(CameraCount).where(CameraCount.analysis_id == analysis_id)) or 0
        if actual != expected_window_count:
            raise Conflict("计数窗口数量不符合预期", actual=actual, expected=expected_window_count)
        analysis.status, analysis.manual_validation = "succeeded", manual_validation
        video = self.session.get(Video, analysis.video_id)
        if video:
            video.status = "ready"
        self.session.flush()
        return public_columns(analysis)

    def query_counts(self, video_id: str, analysis_id: str) -> dict[str, Any]:
        analysis = self.get_or_404(VideoAnalysis, analysis_id, "录像分析")
        if analysis.video_id != video_id:
            raise NotFound("该录像没有指定分析")
        if analysis.status != "succeeded":
            raise AnalysisNotReady("录像分析尚未完成")
        rows = self.session.scalars(select(CameraCount).where(CameraCount.analysis_id == analysis_id).order_by(CameraCount.window_start)).all()
        return {"analysis": public_columns(analysis), "points": [public_columns(x) for x in rows]}


class ForecastRepository(Repository):
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
    def __init__(self, session: Session, writable: bool) -> None:
        self.session = session
        self.auth = AuthRepository(session, writable)
        self.catalog = CatalogRepository(session, writable)
        self.provenance = ProvenanceRepository(session, writable)
        self.observations = ObservationRepository(session, writable)
        self.jobs = JobRepository(session, writable)
        self.holidays = HolidayRepository(session, writable)
        self.events = EventRepository(session, writable)
        self.knowledge = KnowledgeRepository(session, writable)
        self.video = VideoRepository(session, writable)
        self.forecasts = ForecastRepository(session, writable)
