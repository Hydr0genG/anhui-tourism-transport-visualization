"""在已迁移的独立验收库导入两次并保存来源追溯报告，不用于生产库。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import inspect, text
from data_foundation import SeriesQuery, UnsupportedGrain, create_store
from data_foundation.ingestion.real_dataset import import_real_dataset


def verify(manifest_path: Path, report_path: Path, *, allow_sqlite: bool):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    store = create_store(os.environ["DATABASE_URL"], data_root=os.environ["DATA_ROOT"], allow_sqlite_for_tests=allow_sqlite)
    try:
        with store.engine.connect() as connection:
            version = connection.scalar(text("SELECT VERSION()" if store.engine.dialect.name == "mysql" else "SELECT sqlite_version()"))
            migration = connection.scalar(text("SELECT version_num FROM alembic_version"))
        first = import_real_dataset(store, manifest_path)
        second = import_real_dataset(store, manifest_path)
        if first["counts"]["quarantined"] or second["counts"]["imported"] or second["counts"]["revised"] or second["documents"]["inserted"] or any(v["inserted"] for v in second["definitions"].values()):
            raise AssertionError("重复导入或质量验收失败")
        samples = []
        with store.read() as uow:
            for row in manifest["observations"]:
                if (row["metric_code"], row["period_start"]) not in {("airport_throughput", "2025-01-01"), ("airport_throughput", "2026-08-01"), ("rail_departures", "2025-04-06")}:
                    continue
                series = uow.observations.query_series(SeriesQuery(row["entity_id"], row["metric_code"], row["grain"],
                    date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"])))
                if len(series["points"]) != 1 or series["points"][0]["value"] != Decimal(row["standard_value"]):
                    raise AssertionError("真实查询数值与原件规范值不一致")
                point = series["points"][0]
                asset = uow.provenance.get_asset(point["provenance"]["asset_id"], internal=True)
                raw = store.data_root / asset["relative_path"]
                if hashlib.sha256(raw.read_bytes()).hexdigest() != asset["sha256"] or point["provenance"]["asset_url"] != asset["url"]:
                    raise AssertionError("查询原件追溯不一致")
                samples.append({"entity_name": series["entity"]["name"], "period_start": row["period_start"],
                    "standard_value": str(point["value"]), "unit": point["unit"], "original_value": row["original_value"],
                    "original_unit": row["original_unit"], "raw_excerpt": row["raw_excerpt"], "provenance": point["provenance"],
                    "sha256": asset["sha256"], "stored_raw_sha256_verified": True})
            if len(samples) < 5:
                raise AssertionError("不足五条来源追溯验收样本")
            for item in manifest["documents"]:
                detail = uow.knowledge.get_document(item["id"])
                if detail["entity_ids"] != sorted(item["entity_ids"]) or detail["asset_id"] != first["asset_ids"][item["asset_key"]]:
                    raise AssertionError("文档详情与资料清单不一致")
                if "relative_path" in detail or "text_path" in detail:
                    raise AssertionError("文档详情泄露内部路径")
            rail_id = next(e["id"] for e in manifest["catalog"]["entities"] if e["type"] == "rail_station")
            try:
                uow.observations.query_series(SeriesQuery(rail_id, "rail_departures", "month", date(2025, 1, 1), date(2025, 6, 30)))
            except UnsupportedGrain:
                unsupported_rejected = True
            else:
                raise AssertionError("铁路月度查询应明确拒绝不支持粒度")
            document_page = uow.knowledge.list_documents()
            definition_audits = {"holidays": [], "events": []}
            if manifest.get("holiday_calendar"):
                calendar_data = json.loads((manifest_path.parent/manifest["holiday_calendar"]["path"]).read_text())
                for holiday in calendar_data["holidays"]:
                    record = uow.holidays.get_holiday(holiday["id"])
                    if record["start_date"].isoformat() != holiday["start_date"] or record["end_date"].isoformat() != holiday["end_date"] or record["definition_asset_id"] != first["asset_ids"][holiday["asset_key"]]:
                        raise AssertionError("假期日历与原件定义不一致")
                    definition_audits["holidays"].append(record)
            for event in manifest.get("events", []):
                record = uow.events.get_event(event["id"])
                if record["status"] != "expired" or record["ends_at"].date() != datetime.fromisoformat(event["ends_at"]).date():
                    raise AssertionError("历史事件日期或状态不一致")
                definition_audits["events"].append(record)
            if manifest.get("events") and uow.events.list_events(status="active", now=datetime.now(timezone.utc)):
                raise AssertionError("历史关闭公告不能作为当前事件")
        quality = json.loads((manifest_path.parent/manifest["quality_report"]["path"]).read_text())
        report = {"dataset_id": manifest["dataset_id"], "verified_at": datetime.now(timezone.utc).isoformat(),
            "environment": "isolated_validation_database", "dialect": store.engine.dialect.name, "database_version": version,
            "migration_head": migration, "schema_table_count": len(inspect(store.engine).get_table_names()) - 1,
            "first_import": first["counts"], "second_import": second["counts"], "documents_first": first["documents"],
            "documents_second": second["documents"], "document_count": document_page["total"],
            "definitions_first": first["definitions"], "definitions_second": second["definitions"], "definition_audits": definition_audits,
            "source_audits": samples, "unsupported_rail_month_rejected": unsupported_rejected,
            "result": "passed", "production_database_verified": False,
            "remaining": quality.get("remaining_gaps", ["机场连续12个月验收未通过：2023—2025年12月单月值未取得", "景区与铁路连续月报未取得", "E知识索引、D正式库HTTP与Worker尚未联调"])}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)+"\n", encoding="utf-8")
        print(json.dumps({"dialect": report["dialect"], "database_version": version, "result": "passed", "observations": first["counts"], "documents": report["document_count"], "source_audits": len(samples)}, ensure_ascii=False))
    finally:
        store.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-sqlite-validation", action="store_true")
    args = parser.parse_args()
    verify(args.manifest.resolve(), args.report.resolve(), allow_sqlite=args.allow_sqlite_validation)
