"""Seal C-only delivery from tracked inputs and explicit acceptance evidence.

Never includes the independent crawler, unrelated expansion work, runtime raw
copies, DB dumps, credentials or previous release archives. Does not publish.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile, ZIP_DEFLATED


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    project = root.parent
    release = root / "release"
    evidence = root / "acceptance/C-contract-closeout-2026-10-17"
    archive = release / "C-contract-closeout-0.2.1a2-2026-10-17.zip"
    receipt_path = archive.with_suffix(".receipt.json")
    assert not archive.exists() and not receipt_path.exists(), "Never overwrite a sealed delivery"
    summary = json.loads((evidence / "release-verification.json").read_text())
    assert summary["status"] == "passed" and summary["test_runs"]["source-tests.xml"]["tests"] == 479
    assert json.loads((evidence / "mysql-shutdown.json").read_text())["shutdown_confirmed"]
    wheel = release / "anhui_data_foundation-0.2.1a2-py3-none-any.whl"
    real = json.loads((evidence / "real-receiving/report.json").read_text())
    assert real["packages"]["C"]["wheel_sha256"] == sha(wheel)
    old = json.loads((release / "C-visible-storage-0.2.1a1-2026-10-16.receipt.json").read_text())
    for item in old["files"]:
        path = release / item["name"]
        if not path.exists():
            path = project / item["name"]
        assert sha(path) == item["sha256"]
    commit = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(["git", "-C", str(project), "diff", "--exit-code", "HEAD", "--",
        "data_foundation/data_foundation", "data_foundation/pyproject.toml", "data_foundation/tests",
        "data_foundation/examples/package_c021a2.py"], check=True, capture_output=True)
    tracked = subprocess.check_output(["git", "-C", str(project), "ls-files", "-z", "--", "data_foundation"]).decode().split("\0")
    members = {}
    for name in filter(None, tracked):
        path = project / name
        relative = path.relative_to(root)
        if (relative.parts[0] in {"data_foundation", "tests", "alembic", "examples", "assets", "real_data"}
                or len(relative.parts) == 1 and relative.suffix in {".md", ".toml", ".ini"}):
            if path.is_file() and "__pycache__" not in path.parts:
                members[name] = path
    for path in evidence.rglob("*"):
        if path.is_file() and "raw" not in path.relative_to(evidence).parts:
            members[path.relative_to(project).as_posix()] = path
    upstream = root / "acceptance/C-DE-readiness-2026-10-16/upstream-e060"
    for path in upstream.iterdir():
        if path.is_file():
            members[path.relative_to(project).as_posix()] = path
    for path in (wheel, release / "README.md", project / "03-接口总览.md",
                 project / "致D和E-今日确认与接收清单-2026-10-17.md",
                 project / "致D和E-C021a1统一存储交接-2026-10-16.md"):
        members[path.relative_to(project).as_posix()] = path
    entries = [dict(path=name, byte_size=path.stat().st_size, sha256=sha(path)) for name, path in sorted(members.items())]
    assert not any("expansion_source_audit" in row["path"] or "anhui_public_data_crawler" in row["path"] for row in entries)
    assert not any(path.suffix in {".sqlite", ".sqlite3", ".sql", ".pyc"} or path.name.startswith(".env") for path in members.values())
    manifest = {"version": "0.2.1a2", "source_commit": commit, "files": entries}
    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        for name, path in sorted(members.items()):
            output.write(path, name)
        output.writestr("DELIVERY-MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    with ZipFile(archive) as output:
        assert output.testzip() is None
    receipt = dict(version="0.2.1a2", source_commit=commit, created_at=datetime.now(timezone.utc).isoformat(),
        published_to_remote=False, verification=summary, archive_members=len(entries) + 1,
        files=[dict(name=path.name, byte_size=path.stat().st_size, sha256=sha(path)) for path in (wheel, archive)],
        exclusions=["Independent crawler", "Unrelated expansion changes", "Business credentials and databases", "Runtime raw copies", "Actual videos and weights"])
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_commit": commit, "files": receipt["files"], "archive_members": len(entries) + 1}, ensure_ascii=False))


if __name__ == "__main__":
    main()
