"""Freeze read-only D/E code evidence from exact fetched Git commits."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

D_COMMIT = "d969e35134d17602b7692831262940fbf35289a0"
E_COMMIT = "784a882188dcf5f767e3d8111fee6926a052b2a9"


def git_bytes(repo, commit, path):
    return subprocess.check_output(["git", "-C", str(repo), "show", commit + ":" + path])


def snippet(body, marker, length=24):
    lines = body.decode("utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if marker in line)
    return {"line": index + 1, "code": "\n".join(lines[index:index + length])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d", type=Path, required=True)
    parser.add_argument("--e", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output directory; do not overwrite evidence")
    findings = []
    cases = [
        ("D", "app/services/c_store.py", "def write_count_points", 64,
         "P2P counts are written in D private storage; no lease parameter, delete/replace by analysis ID"),
        ("D", "app/services/c_store.py", "def complete_video_analysis", 36,
         "Analysis publication has no lease parameter and commits independently of job completion"),
        ("D", "app/worker.py", "def _run_video_analysis", 82,
         "Count writes and analysis success precede outer complete_job; not one atomic publication"),
        ("D", "app/services/e_bindings.py", "def knowledge_snapshot_loader", 63,
         "Normal branch uses E verified loader; ImportError branch still allows weaker snapshot"),
        ("D", "app/services/metrics_query.py", "def metric_tool", 90,
         "All metric branches are still explicit unavailable placeholders; C query adapter not wired"),
        ("D", "app/worker.py", "document_version = (", 25,
         "Normal worker separates source version from new index version; fallback behavior needs review"),
        ("D", "app/worker.py", "def _locate_staging_file", 28,
         "Staging input selected by filename; source SHA/asset binding not established here"),
        ("E", "intelligent_analysis/service.py", "def index_document", 18,
         "Source and index versions are coupled in service submission, but worker supports separation"),
        ("E", "intelligent_analysis/worker/knowledge_worker.py", "def process_knowledge_job", 29,
         "Worker reads document_version separately from version/index_version"),
    ]
    for side, path, marker, length, interpretation in cases:
        repo, commit = (args.d, D_COMMIT) if side == "D" else (args.e, E_COMMIT)
        raw = git_bytes(repo, commit, path)
        findings.append({"side": side, "commit": commit, "path": path,
                         "sha256": hashlib.sha256(raw).hexdigest(), "interpretation": interpretation,
                         **snippet(raw, marker, length)})
    args.output.mkdir(parents=True)
    upstream = args.output / "upstream-e060"
    upstream.mkdir()
    paths = ["docs/致D-整合回执答复与故障复现-2026-10-15.md",
             "docs/acceptance/E060-D-reply-failure-recheck-2026-10-15.json",
             "release/E-visible-people-0.6.0-2026-10-15.receipt.json",
             "release/shumei_intelligent_analysis-0.6.0-py3-none-any.whl"]
    saved = []
    for path in paths:
        raw = git_bytes(args.e, E_COMMIT, path)
        target = upstream / Path(path).name
        target.write_bytes(raw)
        saved.append({"path": path, "local_name": target.name, "byte_size": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest(), "commit": E_COMMIT})
    report = {"status": "reviewed_not_runtime_accepted", "d_commit": D_COMMIT, "e_commit": E_COMMIT,
              "repository_sources": {"D": "https://gitee.com/kokoski/dpart", "E": "https://github.com/w3022843643/shumei_intelligent_analysis"},
              "findings": findings, "upstream_files": saved,
              "d_machine_json_evidence_received": False, "d_or_e_source_modified": False}
    (args.output / "D-E-source-review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "findings": len(findings), "upstream_files": len(saved)}))


if __name__ == "__main__":
    main()
