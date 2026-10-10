from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..contracts import ObservationInput
from ..errors import DataFoundationError, InvalidArgument
from ..store import DataStore


REQUIRED_COLUMNS = {
    "entity_id", "metric_code", "period_start", "period_end", "grain",
    "original_value", "original_unit", "standard_value", "standard_unit",
    "conversion_factor", "source_locator", "methodology_version",
}


def _optional_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidArgument("published_at 必须带时区")
    return parsed


def _parse_row(row: dict[str, str], *, source_id: str, asset_id: str) -> ObservationInput:
    try:
        return ObservationInput(
            entity_id=row["entity_id"].strip(),
            metric_code=row["metric_code"].strip(),
            period_start=date.fromisoformat(row["period_start"].strip()),
            period_end=date.fromisoformat(row["period_end"].strip()),
            grain=row["grain"].strip(),
            value=Decimal(row["standard_value"].strip()),
            unit=row["standard_unit"].strip(),
            original_value=row["original_value"].strip(),
            original_unit=row["original_unit"].strip(),
            conversion_factor=Decimal(row["conversion_factor"].strip()),
            source_id=source_id,
            asset_id=asset_id,
            source_locator=row["source_locator"].strip(),
            methodology_version=row["methodology_version"].strip(),
            published_at=_optional_datetime(row.get("published_at")),
            quality_status=(row.get("quality_status") or "verified").strip(),
        )
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise InvalidArgument("CSV 行格式或数值无效", error=str(exc)) from exc


def import_standard_csv(
    store: DataStore, csv_path: str | Path, *, source_id: str, asset_id: str,
    dry_run: bool = True, reason: str = "标准 CSV 导入",
) -> dict[str, Any]:
    """导入经过人工核验的规范 CSV；逐行隔离问题，整份文件使用一个事务。"""
    path = Path(csv_path)
    if not path.is_file():
        raise InvalidArgument("CSV 文件不存在", path=str(path))

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - fields)
        if missing:
            raise InvalidArgument("CSV 缺少必填列", missing=missing)
        rows = list(reader)
    if len(rows) > 100_000:
        raise InvalidArgument("单个原件超过 100000 行，请先调整范围")

    counts = {"processed": 0, "imported": 0, "revised": 0, "skipped": 0, "quarantined": 0}
    with store.write() as uow:
        run = uow.observations.start_run(
            source_id=source_id, asset_id=asset_id, dry_run=dry_run, reason=reason,
        )
        for number, raw in enumerate(rows, start=2):
            counts["processed"] += 1
            try:
                data = _parse_row(raw, source_id=source_id, asset_id=asset_id)
                with uow.session.begin_nested():
                    action, _ = uow.observations.add_or_revise(data, run.id, dry_run=dry_run)
                counts[action] += 1
            except (DataFoundationError, ValueError) as exc:
                counts["quarantined"] += 1
                uow.observations.add_quality_issue(
                    run.id, row_locator=f"CSV row {number}", code=getattr(exc, "code", "invalid_row"),
                    message=str(exc), raw_payload={key: value for key, value in raw.items() if key != "password"},
                )
        summary = uow.observations.finish_run(run.id, counts=counts)
    return {"run": summary, "counts": counts, "dry_run": dry_run}
