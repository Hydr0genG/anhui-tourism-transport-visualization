from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from .db.models import IngestionRun, Observation, QualityIssue, Source
from .ingestion import import_standard_csv
from .seed import seed_catalog
from .store import create_store


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _store():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("请先设置 DATABASE_URL")
    return create_store(database_url, data_root=os.getenv("DATA_ROOT"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="data-foundation")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查数据库连接和迁移外部条件")
    seed = sub.add_parser("seed-catalog", help="写入指标字典及已核验的实体/来源台账")
    seed.add_argument("--assets-dir", type=Path, help="可选：使用项目内已核验台账目录")
    ingest = sub.add_parser("ingest-csv", help="导入规范 CSV；默认 dry-run")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--source-id", required=True)
    ingest.add_argument("--asset-id", required=True)
    ingest.add_argument("--apply", action="store_true")
    ingest.add_argument("--reason", default="标准 CSV 导入")
    sub.add_parser("quality-report", help="输出当前覆盖和质量摘要 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _store()
    try:
        if args.command == "doctor":
            result = store.doctor()
        elif args.command == "seed-catalog":
            result = seed_catalog(store, args.assets_dir)
        elif args.command == "ingest-csv":
            result = import_standard_csv(
                store, args.path, source_id=args.source_id, asset_id=args.asset_id,
                dry_run=not args.apply, reason=args.reason,
            )
        else:
            with store.read() as uow:
                result = {
                    "report_version": "v0.1.0",
                    "sources": uow.session.scalar(select(func.count()).select_from(Source)) or 0,
                    "observations": uow.session.scalar(select(func.count()).select_from(Observation)) or 0,
                    "open_quality_issues": uow.session.scalar(
                        select(func.count()).select_from(QualityIssue).where(QualityIssue.status == "open")
                    ) or 0,
                    "ingestion_runs": uow.session.scalar(select(func.count()).select_from(IngestionRun)) or 0,
                }
        print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
        return 0
    finally:
        store.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
