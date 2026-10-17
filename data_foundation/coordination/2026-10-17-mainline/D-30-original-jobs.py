"""作业接口（03-接口总览 §9：29 / 30）。

29 已接线：走 C 的 ``uow.jobs.enqueue_or_get``。D 只**入队**，不代跑——
导入由 C 的 worker 领租约后执行，结果看 30。此前做成 503「等 C」是过时判断。

30 查的是 D 自己的作业桥接库（knowledge_index / video_analysis 等 D/E 类作业）；
C 的 ingestion 作业目前不在其中，等 30 接 C 的 jobs 表后统一。
"""

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.api.v1._c_errors import raise_mapped_c_error
from app.core.errors import CODE_BAD_REQUEST, CODE_NOT_FOUND, AppError
from app.core.permissions import require_admin, require_csrf
from app.core.providers import get_c_store, get_job_store, require_data_foundation
from app.schemas.common import Envelope, envelope
from app.schemas.jobs import IngestionAccepted, IngestionRequest, JobOut

router = APIRouter(prefix="/jobs", tags=["作业"])

# 03-接口总览 §1 通用约定：接口时间输出统一为东八区（+08:00）ISO 8601
_CN_TZ = timezone(timedelta(hours=8))


def _to_cn_tz(value: str | None) -> str | None:
    """作业库按 UTC 存储（04 文档 §4），输出时转为 +08:00。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(_CN_TZ).isoformat()
    except ValueError:
        return value


@router.post("/ingestion", status_code=202, response_model=IngestionAccepted)
async def trigger_ingestion(
    body: IngestionRequest,
    session=Depends(require_admin),
    _csrf=Depends(require_csrf),  # 03 契约 §29：写操作要求 X-CSRF-Token
    _dep=Depends(require_data_foundation),
) -> IngestionAccepted:
    """29 POST /jobs/ingestion：管理员；幂等导入，无新数据时 imported_count=0。

    D **只入队**，不代跑：导入由 C 的 worker 领租约执行，执行结果看 30。
    定时任务调用同一业务服务，但不通过浏览器会话（所以 CSRF 只在本路由要求）。
    """
    for source_id in body.source_ids:
        try:
            UUID(source_id)
        except (ValueError, AttributeError, TypeError):
            raise AppError(400, CODE_BAD_REQUEST,
                           f"source_ids 含非法的 UUID：{source_id}") from None

    cstore = get_c_store()
    try:
        job = cstore.enqueue_ingestion(
            idempotency_key=_idempotency_key(body),
            payload={"source_ids": sorted(set(body.source_ids)),
                     "dry_run": body.dry_run},
            reason=body.reason,
            requested_by=session.user_id,
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001 —— 统一映射 C 的异常
        raise_mapped_c_error(exc, "采集作业入队")
        raise
    return IngestionAccepted(job_id=job["id"], status=job["status"])


def _idempotency_key(body: IngestionRequest) -> str:
    """幂等键：同一批来源 + 同一个 dry_run 视为同一个作业。

    C 的 ``enqueue_or_get`` 用 ``(type, idempotency_key)`` 唯一约束，
    命中既有作业就直接返回它（**不再重复排队**）。键里刻意**不含 reason**——
    理由只是留痕，改一句话不该变成另一个作业；也刻意不含时间，否则永远命中不了。
    """
    raw = "|".join([*sorted(set(body.source_ids)), "1" if body.dry_run else "0"])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@router.get("/{job_id}", response_model=Envelope[JobOut])
async def get_job(
    job_id: str,
    request: Request,
    _admin=Depends(require_admin),
) -> dict:
    """30 GET /jobs/{job_id}：管理员；作业状态查询，不返回敏感日志。

    当前从 D 的作业桥接库查询（knowledge_index / video_analysis 等 D/E 类作业）；
    C 的 ingestion 作业在 C 的正式 jobs 表落地后一并接入。
    """
    try:  # 03 契约：job_id 为 UUID；非 UUID 属参数错误（400）
        UUID(job_id)
    except ValueError:
        raise AppError(400, CODE_BAD_REQUEST, f"job_id 不是合法的 UUID：{job_id}") from None

    job = get_job_store().get_job(job_id)
    if job is None:
        raise AppError(404, CODE_NOT_FOUND, "作业不存在")
    return envelope(
        request,
        JobOut(
            id=job["id"],
            type=job["type"],
            status=job["status"],
            started_at=_to_cn_tz(job["started_at"]),
            finished_at=_to_cn_tz(job["finished_at"]),
            processed_count=job["processed_count"],
            imported_count=job["imported_count"],
            error_summary=job["error_summary"],
            analysis_id=job["analysis_id"],
        ).model_dump(mode="json"),
    )
