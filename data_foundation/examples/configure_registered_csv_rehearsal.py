"""Configure one existing-C-data CSV input in an explicitly selected test DB.

Default is local file validation only. --apply requires separate test-only DB
and DATA_ROOT environment variables; never falls back to business DATABASE_URL.
Does not submit/execute jobs, fetch URLs, or change the sealed C wheel.
"""
import argparse
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re

from sqlalchemy.engine import make_url

from data_foundation import AssetCreate, Conflict, InvalidArgument, create_store
from data_foundation.artifacts import verify_artifact
from data_foundation.db.models import Source


def validate_bundle(bundle):
    manifest = json.loads((bundle / "input-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "C-registered-csv-rehearsal-1":
        raise InvalidArgument("Unknown CSV rehearsal manifest")
    content = {}
    for key in ("csv", "evidence"):
        entry = manifest[key]
        relative = Path(entry["path"])
        if relative.name != entry["path"] or relative.is_absolute():
            raise InvalidArgument("Input must be a direct bundle member")
        raw = (bundle / relative).read_bytes()
        if len(raw) != entry["byte_size"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise Conflict("CSV/evidence bytes do not match the manifest")
        content[key] = raw
    return manifest, content


def configure(store, bundle):
    manifest, content = validate_bundle(bundle)
    with store.read() as uow:
        source = uow.catalog.get_source(manifest["source_id"])
    if source["adapter_key"] != "standard_csv" or source["verification_status"] != "verified":
        raise InvalidArgument("Existing source must be verified standard_csv; import the C dataset first")
    staged = {key: store.persist_file(source_id=source["id"], original_name=manifest[key]["path"],
                stream=BytesIO(content[key])) for key in ("csv", "evidence")}
    original = manifest["evidence"]
    with store.write() as uow:
        current = uow.provenance.locked(Source, source["id"], "来源")
        if any(getattr(current, key) != source[key] for key in ("adapter_config", "adapter_key", "verification_status")):
            raise Conflict("Source configuration changed while preparing input")
        registered = {}
        for key in ("csv", "evidence"):
            registered[key] = uow.provenance.register_asset(AssetCreate(
                source_id=source["id"], url=original["url"],
                title=original["title"] if key == "evidence" else "C核验规范CSV：合肥机场2025-01（非官网CSV原件）",
                retrieved_at=datetime.fromisoformat(original["retrieved_at"]),
                relative_path=staged[key]["relative_path"], sha256=staged[key]["sha256"],
                media_type=original["media_type"] if key == "evidence" else "text/csv",
                byte_size=staged[key]["byte_size"], usage_note=original["usage_note"],
                published_at=datetime.fromisoformat(original["published_at"])))
            item = registered[key]
            verify_artifact(store.data_root, item["relative_path"], item["sha256"], item["byte_size"])
        config = {**(current.adapter_config or {}), "csv_asset_id": registered["csv"]["id"],
                  "provenance_asset_id": registered["evidence"]["id"]}
        current.adapter_config = config
    return {"status": "configured_test_input", "source_id": source["id"],
            "csv_asset_id": registered["csv"]["id"], "provenance_asset_id": registered["evidence"]["id"],
            "expected_value": manifest["expected_value"], "rows": 1,
            "HTTP_executed": False, "job_executed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest, _ = validate_bundle(args.bundle)
    if not args.apply:
        print(json.dumps({"status": "validated_only", "writes": False,
            "source_id": manifest["source_id"], "rows": 1, "expected_value": manifest["expected_value"]}))
        return
    value = os.environ.get("C_REGISTERED_CSV_TEST_DATABASE_URL", "")
    parsed = make_url(value)
    if parsed.drivername != "mysql+pymysql" or not (parsed.database == "tourism_flow_test" or
            re.fullmatch(r"cde_readiness_[a-z0-9_]+_test", parsed.database or "")):
        parser.error("--apply only accepts tourism_flow_test or dedicated cde_readiness_*_test MySQL DB")
    data_root = os.environ.get("C_REGISTERED_CSV_TEST_DATA_ROOT")
    if not data_root:
        parser.error("Set a dedicated C_REGISTERED_CSV_TEST_DATA_ROOT; no business DATA_ROOT fallback")
    store = create_store(value, data_root=data_root)
    try:
        print(json.dumps(configure(store, args.bundle), ensure_ascii=False))
    finally:
        store.dispose()


if __name__ == "__main__":
    main()
