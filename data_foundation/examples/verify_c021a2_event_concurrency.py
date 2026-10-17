"""Two real MySQL connections compete on one synthetic event/version.

Run only against a fresh cde_readiness_*_test schema. Retain DB files; no DROP.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from threading import Barrier
from uuid import uuid4

from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from data_foundation import AssetCreate, Conflict, SourceCreate, create_store
from data_foundation.db import Base


def main():
    url = os.environ["C_CLOSEOUT_DATABASE_URL"]
    parsed = make_url(url)
    if parsed.drivername != "mysql+pymysql" or not re.fullmatch(r"cde_readiness_[a-z0-9_]+_test", parsed.database or ""):
        raise ValueError("Use a dedicated empty closeout test schema")
    store = create_store(url)
    try:
        assert not inspect(store.engine).get_table_names(), "Never clear an existing schema"
        Base.metadata.create_all(store.engine)
        source_id = str(uuid4())
        now = datetime.now(timezone.utc)
        with store.write() as uow:
            uow.provenance.register_source(SourceCreate(source_id, "SYNTHETIC", "Concurrency only", "https://example.invalid", "test", "irregular"))
            asset = uow.provenance.register_asset(AssetCreate(source_id, "https://example.invalid/test", "SYNTHETIC", now,
                "synthetic/no-file-read.txt", "a" * 64, "text/plain", 0, "NOT actual official evidence"))
            event = uow.events.create_event(dict(title="SYNTHETIC", description="Concurrency fixture", kind="other",
                authority="official", severity="notice", region_code="340000", starts_at=now,
                source_id=source_id, asset_id=asset["id"]), operator_id=None, now=now)
        barrier = Barrier(2)
        def update(label):
            try:
                with store.write() as uow:
                    assert uow.events.get_event(event["id"])["row_version"] == 1
                    barrier.wait(timeout=10)  # Both sessions retain an old read snapshot.
                    result = uow.events.update_event(event["id"], dict(description=label, change_note=label),
                        expected_version=1, operator_id=None, now=now)
                return {"status": "succeeded", "version": result["row_version"]}
            except Conflict as exc:
                return {"status": "conflict", "current_version": exc.details["current_version"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(update, "synthetic-worker-" + str(i)) for i in range(2)]
            results = [future.result(timeout=30) for future in tasks]
        assert sorted(result["status"] for result in results) == ["conflict", "succeeded"]
        with store.read() as uow:
            final = uow.events.get_event(event["id"])
        assert final["row_version"] == 2 and len(final["timeline"]) == 2
        report = {"status": "passed", "scope": "synthetic_two_connection_MySQL_concurrency",
            "outcomes": results, "final_row_version": 2, "history_rows": 2,
            "business_db_touched": False, "real_event_created": False}
        output = Path(sys.argv[1])
        assert not output.exists(), "Never overwrite evidence"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        store.dispose()


if __name__ == "__main__":
    main()
