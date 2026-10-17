"""Consume only C ingestion jobs for registered, reviewed local CSV inputs.

No network fetching, discovery, independent crawler DB, or D/E job consumption.
Source adapter_config is server-managed, never a path from an HTTP job payload.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
from io import StringIO

from ..artifacts import verify_artifact
from ..contracts import JobLease
from ..db.models import Source, SourceAsset
from ..errors import Conflict, DataFoundationError, InvalidArgument, LeaseLost
from ..repositories import public_columns
from .standard_csv import REQUIRED_COLUMNS, _parse_row


def _now():
    return datetime.now(timezone.utc)


def _prepare(store, payload):
    if not isinstance(payload, dict) or set(payload) - {"source_ids", "dry_run"}:
        raise InvalidArgument("导入任务只接受source_ids和dry_run")
    source_ids = payload.get("source_ids")
    if (not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 50
            or any(not isinstance(value, str) or not value for value in source_ids)):
        raise InvalidArgument("source_ids必须为1—50个已登记来源")
    dry_run = payload.get("dry_run", True)
    if type(dry_run) is not bool:
        raise InvalidArgument("dry_run必须为布尔值")
    prepared = []
    row_count = 0
    for source_id in sorted(set(source_ids)):
        with store.read() as uow:
            source = uow.catalog.get_source(source_id)
            config = source["adapter_config"]
            if source["adapter_key"] != "standard_csv" or source["verification_status"] != "verified":
                raise InvalidArgument("当前导入仅支持已核验standard_csv来源", source_id=source_id)
            if not isinstance(config, dict) or not config.get("csv_asset_id"):
                raise InvalidArgument("来源尚未配置已登记CSV原件", source_id=source_id)
            csv_asset = uow.provenance.get_asset(config["csv_asset_id"], internal=True)
            asset = uow.provenance.get_asset(config.get("provenance_asset_id", csv_asset["id"]), internal=True)
            if csv_asset["source_id"] != source_id or asset["source_id"] != source_id:
                raise InvalidArgument("CSV及证据原件必须属于同一来源")
        if csv_asset["media_type"] not in {"text/csv", "application/csv"}:
            raise InvalidArgument("csv_asset_id必须指向CSV资产")
        if csv_asset["byte_size"] > 32 * 1024 * 1024:
            raise InvalidArgument("单份CSV最多32MiB")
        verified = verify_artifact(store.data_root, csv_asset["relative_path"], csv_asset["sha256"], csv_asset["byte_size"])
        evidence = verify_artifact(store.data_root, asset["relative_path"], asset["sha256"], asset["byte_size"])
        content = (store.data_root / verified.relative_path).read_bytes()
        if hashlib.sha256(content).hexdigest() != csv_asset["sha256"]:
            raise Conflict("CSV在准备过程中发生变化")
        verified.ensure_unchanged(store.data_root)
        try:
            reader = csv.DictReader(StringIO(content.decode("utf-8-sig"), newline=""), strict=True)
            if not REQUIRED_COLUMNS <= set(reader.fieldnames or []):
                raise InvalidArgument("CSV缺少必填列")
            rows = []
            for row in reader:
                row_count += 1
                if row_count > 100_000:
                    raise InvalidArgument("一个导入任务最多100000行")
                rows.append(row)
        except (UnicodeError, csv.Error) as exc:
            raise InvalidArgument("CSV必须为有效UTF-8规范表格") from exc
        prepared.append((source, csv_asset, asset, verified, evidence, rows))
    return prepared, dry_run


def run_ingestion_once(store, *, worker_id="c-ingestion-worker", job_id=None, lease_seconds=600):
    """Claim one ingestion job and atomically publish all imports and completion.

An expired lease raises LeaseLost; it never fails or completes a newer attempt.
Returns None when no matching queued job exists. Input files are verified and
parsed outside the publication transaction; publication is bounded at 100k rows.
"""
    with store.write() as uow:
        claimed = uow.jobs.claim_job(worker_id, _now(), lease_seconds, job_type="ingestion", job_id=job_id)
    if claimed is None:
        return None
    job = claimed["job"]
    lease = JobLease(**claimed["lease"])
    try:
        prepared, dry_run = _prepare(store, job["payload"])
        counts = dict(processed=0, imported=0, revised=0, skipped=0, quarantined=0)
        run_ids = []
        with store.write() as uow:
            attempt = uow.jobs.ensure_lease(lease)
            # Lock all metadata in stable source order before any observations.
            for source, csv_asset, asset, verified, evidence, rows in prepared:
                current = uow.observations.locked(Source, source["id"], "来源")
                if public_columns(current) != source:
                    raise Conflict("导入准备后来源登记发生变化")
                for item in sorted({csv_asset["id"]: csv_asset, asset["id"]: asset}.values(), key=lambda x: x["id"]):
                    current_asset = uow.observations.locked(SourceAsset, item["id"], "原件")
                    if public_columns(current_asset) != item:
                        raise Conflict("导入准备后原件登记发生变化")
                verified.ensure_unchanged(store.data_root)
                evidence.ensure_unchanged(store.data_root)
            for source, csv_asset, asset, verified, evidence, rows in prepared:
                run = uow.observations.start_run(source_id=source["id"], asset_id=asset["id"],
                    dry_run=dry_run, reason=job["reason"], job_id=job["id"], attempt_id=attempt.id)
                local = dict.fromkeys(counts, 0)
                for number, raw in enumerate(rows, start=2):
                    local["processed"] += 1
                    try:
                        data = _parse_row(raw, source_id=source["id"], asset_id=asset["id"])
                        with uow.session.begin_nested():
                            action, _ = uow.observations.add_or_revise(data, run.id, dry_run=dry_run)
                        local[action] += 1
                    except (DataFoundationError, ValueError) as exc:
                        # Lease errors are never data quality issues.
                        if isinstance(exc, LeaseLost):
                            raise
                        local["quarantined"] += 1
                        uow.observations.add_quality_issue(run.id, row_locator=f"CSV row {number}",
                            code=getattr(exc, "code", "invalid_row"), message=str(exc), raw_payload=None)
                uow.observations.finish_run(run.id, counts=local)
                run_ids.append(run.id)
                for key, value in local.items():
                    counts[key] += value
            result = {"run_ids": run_ids, "counts": counts, "dry_run": dry_run}
            uow.jobs.update_progress(lease, processed_count=counts["processed"],
                imported_count=0 if dry_run else counts["imported"], now=_now())
            uow.jobs.complete_job(lease, result, _now())
        return {"job_id": job["id"], "status": "succeeded", **result}
    except LeaseLost:
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, DataFoundationError) else "ingestion_failed"
        with store.write() as uow:
            uow.jobs.fail_job(lease, code + ": 导入未发布，请检查来源配置或输入", _now())
        return {"job_id": job["id"], "status": "failed", "error_code": code}
