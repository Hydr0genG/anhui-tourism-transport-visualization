"""Read-only D audit at an explicit pushed commit; no runtime claims."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--d", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "Never overwrite evidence"
    commit = subprocess.check_output(["git", "-C", str(args.d), "rev-parse", args.commit], text=True).strip()
    findings = []
    cases = [
        ("app/services/c_store.py", "def write_count_points", 21, "D_private_count_writes_without_C_lease", "Switch new results to C visible.write_points; do not delete and replace prior points"),
        ("app/services/c_store.py", "def complete_video_analysis", 17, "D_private_analysis_publication", "Publish and complete job atomically using C visible.complete_analysis"),
        ("app/services/e_bindings.py", "except ImportError:", 14, "loader_dependency_failure_allows_loose_snapshot", "Use explicit dependency failure instead of snapshot fallback"),
        ("app/services/metrics_query.py", "def metric_tool", 14, "metric_resolver_still_unavailable", "Bind C authorized query_series adapter and verify a real historical metric answer"),
        ("app/api/v1/holidays.py", 'if mode == "daily_average":', 19, "partial_daily_average_fallback", "Use exact_total or complete daily_sum divided by holiday days; never average a partial set"),
        ("app/api/v1/holidays.py", "source_ids=_source_ids", 2, "holiday_total_sources_omitted", "Include exact_total.source_ids when total/average uses holiday aggregate"),
        ("app/api/v1/events.py", "expected = body.expected_version", 5, "missing_version_automatically_filled", "Require explicit expected_version and return row_version"),
        ("app/worker.py", "claimed = self.store.claim_job", 7, "worker_can_claim_unsupported_C_ingestion", "Filter D job types; C run-ingestion-once owns registered local CSV jobs"),
        ("app/worker.py", "def _locate_staging_file", 14, "reindex_file_not_bound_to_registered_original", "Use C get_document_file_ref or equally verified immutable bytes"),
    ]
    for name, marker, size, code, action in cases:
        raw = subprocess.check_output(["git", "-C", str(args.d), "show", commit + ":" + name])
        lines = raw.decode().splitlines()
        matches = [i for i, line in enumerate(lines) if marker in line]
        # ImportError occurs elsewhere too; match the branch in the strict-loader function.
        if code.startswith("loader_"):
            begin = next(i for i, line in enumerate(lines) if line.startswith("def knowledge_snapshot_loader"))
            matches = [i for i in matches if i > begin]
        line = matches[0]
        findings.append(dict(path=name, line=line + 1, file_sha256=hashlib.sha256(raw).hexdigest(),
            code=code, required_action=action, excerpt="\n".join(lines[line:line+size])))
    report = dict(status="reviewed_not_runtime_accepted", reviewed_at=datetime.now(timezone.utc).isoformat(),
        D_commit=commit, source="https://gitee.com/kokoski/dpart", findings=findings,
        D_source_modified=False, E_source_modified=False, HTTP_executed=False,
        D_original_JSON_evidence_received=False)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"D_commit": commit, "findings": len(findings), "runtime_accepted": False}))


if __name__ == "__main__":
    main()
