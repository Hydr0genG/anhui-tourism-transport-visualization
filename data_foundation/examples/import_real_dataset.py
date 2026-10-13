"""默认只校验；--apply 显式写入 DATABASE_URL/ DATA_ROOT 指定的库与目录。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_foundation import create_store
from data_foundation.ingestion.real_dataset import import_real_dataset, validate_real_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-sqlite-validation", action="store_true", help="SQLite只用于存储验证，不代表正式库验收")
    args = parser.parse_args()
    result = validate_real_dataset(args.manifest)
    if args.apply:
        if not os.getenv("DATABASE_URL") or not os.getenv("DATA_ROOT"):
            raise SystemExit("--apply 必须配置 DATABASE_URL 和 DATA_ROOT；先完成空库迁移。")
        store = create_store(os.environ["DATABASE_URL"], data_root=os.environ["DATA_ROOT"],
                             allow_sqlite_for_tests=args.allow_sqlite_validation)
        try:
            result = import_real_dataset(store, args.manifest)
        finally:
            store.dispose()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["status"] != "partial_import" else 1


if __name__ == "__main__":
    raise SystemExit(main())
