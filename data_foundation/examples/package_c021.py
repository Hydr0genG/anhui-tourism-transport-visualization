"""Create the immutable C021 handoff from explicitly scoped, verified files."""
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def main():
    root=Path(__file__).resolve().parents[1]
    project=root.parent
    out=root/"acceptance/C-DE-readiness-2026-10-16"
    release=root/"release"
    wheel=release/"anhui_data_foundation-0.2.1a1-py3-none-any.whl"
    archive=release/"C-visible-storage-0.2.1a1-2026-10-16.zip"
    receipt_path=release/"C-visible-storage-0.2.1a1-2026-10-16.receipt.json"
    assert not archive.exists() and not receipt_path.exists(),"Never overwrite a delivery"
    assert sha(release/"anhui_data_foundation-0.2.0a2-py3-none-any.whl")=="4cf48f5c8c4303e2cb73a4169d62dd6111dba03df266256eced48ec565590fbe"
    assert sha(release/"C-index-artifacts-0.2.0a2-2026-10-14.zip")=="7fcbbc0031a13520b5d02aa9ebc8f9066d7b5a237609b0b2c6b0a87a96e54ad5"
    verified=json.loads((out/"mysql-upgrade/report.json").read_text())
    assert verified["status"]=="passed" and verified["packages"]["C"]["wheel_sha256"]==sha(wheel)
    assert verified["business_tables"]==34 and len(verified["checks"])==12
    tests={}
    for name,expected in (("source-tests.xml",425),("installed-wheel-tests.xml",425),
                          ("mysql-tests.xml",68),("mysql-installed-wheel-tests.xml",69)):
        suite=ET.parse(out/name).getroot().find("testsuite")
        counts={k:int(suite.attrib[k]) for k in ("tests","failures","errors","skipped")}
        assert counts=={"tests":expected,"failures":0,"errors":0,"skipped":0}
        tests[name]=counts
    server=Path("/private/tmp/cde-readiness.0JoRzH")
    assert not (server/"mysql.sock").exists() and not (server/"mysql.pid").exists()
    assert "Shutdown complete" in (server/"server.log").read_text()
    # Ensure current source, the installed code, and the official wheel agree.
    import data_foundation
    installed=Path(data_foundation.__file__).resolve().parent
    assert "site-packages" in installed.parts
    with ZipFile(wheel) as z:
        for name in z.namelist():
            if name.startswith("data_foundation/") and name.endswith((".py",".json")):
                relative=Path(name).relative_to("data_foundation")
                assert (root/"data_foundation"/relative).read_bytes()==z.read(name)==(installed/relative).read_bytes()
    source_commit=subprocess.check_output(["git","-C",str(project),"rev-parse","HEAD"],text=True).strip()
    summary={"status":"passed","version":"0.2.1a1","migration_head":"0007","business_tables":34,
        "source_commit":source_commit,"test_runs":tests,"real_C_E_checks":12,"packages":verified["packages"],
        "temporary_mysql_shutdown_confirmed":True,"files_or_databases_deleted":False,
        "old_C_a2_packages_unchanged":True,"D_source_modified":False,"E_source_modified":False,
        "D_runtime_executed":False,"HTTP_executed":False,"Windows_executed":False,
        "real_video_inference":False,"real_manual_MAE":False,"crawler_modified_by_this_work":False,
        "pending":["D C021 adapter and HTTP receipt","D three original JSON evidence files",
                   "Authorized real recording and actual recording times","E Windows receiving",
                   "Real annotation and prediction validation"]}
    save(out/"release-verification.json",summary)
    save(out/"mysql-shutdown.json",{"shutdown_confirmed":True,"network_listener_enabled":False,
         "temporary_instance_only":True,"database_files_retained":True,"data_deleted":False})
    save(out/"runtime-versions.json",{d.metadata["Name"]:d.version for d in importlib.metadata.distributions()})
    tracked=[name for name in subprocess.check_output(
        ["git","-C",str(project),"ls-files","-z","--","data_foundation"]).decode("utf-8").split("\0") if name]
    files=set()
    for name in tracked:
        relative=Path(name).relative_to("data_foundation")
        if relative.parts[0] in {"assets","examples","real_data"} and (root/relative).is_file(): files.add(root/relative)
        elif relative.suffix==".md" and len(relative.parts)==1: files.add(root/relative)
    for folder in ("data_foundation","alembic","tests"):
        files.update(p for p in (root/folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix!=".pyc")
    files.update(root/p for p in ("README.md","pyproject.toml","alembic.ini","26-P2PNet统一存储与021a1交接.md"))
    files.update(root/"examples"/name for name in ("c_readonly_metric_tool.py","c_document_input.py","review_latest_de.py",
                  "verify_c021_upgrade.py","package_c021.py"))
    files.update(p for p in out.rglob("*") if p.is_file() and "raw" not in p.relative_to(out).parts
                 and p.name!="source-tests-initial-failure.xml")
    files.add(wheel)
    files.add(release/"README.md")
    handoff=project/"致D和E-C021a1统一存储交接-2026-10-16.md"
    members={"data_foundation/"+p.relative_to(root).as_posix():p for p in files}
    members[handoff.name]=handoff
    entries=[{"path":name,"byte_size":path.stat().st_size,"sha256":sha(path)} for name,path in sorted(members.items())]
    assert not any("anhui_public_data_crawler" in e["path"] or "expansion_source_audit" in e["path"] for e in entries)
    with ZipFile(archive,"w",ZIP_DEFLATED) as z:
        for name,path in sorted(members.items()): z.write(path,name)
        z.writestr("DELIVERY-MANIFEST.json",json.dumps({"version":"0.2.1a1","source_commit":source_commit,"files":entries},ensure_ascii=False,indent=2)+"\n")
    with ZipFile(archive) as z:
        assert z.testzip() is None
        assert not any(n.endswith((".sqlite",".sqlite3","/.env")) or "/.git/" in n for n in z.namelist())
    receipt={"version":"0.2.1a1","created_at":datetime.now(timezone.utc).isoformat(),"source_commit":source_commit,
        "published_to_remote_at_packaging":False,"files":[{"name":p.name,"byte_size":p.stat().st_size,"sha256":sha(p)} for p in (wheel,archive,handoff)],
        "verification":summary,"archive_members":len(entries)+1,
        "exclusions":["Crawler project","Business credentials/DB dumps","Actual video/weights","Runtime raw copies","Initial local diagnostic paths"]}
    save(receipt_path,receipt)
    print(json.dumps({"status":"packaged","source_commit":source_commit,"files":receipt["files"],"members":receipt["archive_members"]},ensure_ascii=False,indent=2))


if __name__=="__main__": main()
