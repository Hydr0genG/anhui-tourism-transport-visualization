"""Official installed-wheel acceptance in an exclusive cde_readiness_*_test DB.

No business DB, real video, D HTTP or Windows acceptance is claimed.
"""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib
import importlib.metadata
import importlib.util
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch
from zipfile import ZipFile

from sqlalchemy import MetaData, create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from data_foundation import Conflict, JobCreate, JobLease, create_store
from data_foundation.ingestion.real_dataset import import_real_dataset
from data_foundation.repositories import JobRepository
from data_foundation.seed import seed_catalog
from intelligent_analysis import ServiceBindings, answer_message, configure_services
from intelligent_analysis.integration import DependencyUnavailable
from intelligent_analysis.knowledge.artifacts import load_verified_knowledge_snapshot
from intelligent_analysis.worker.knowledge_worker import process_knowledge_job


def encode(value):
    if isinstance(value,(datetime,date)): return value.isoformat()
    if isinstance(value,Decimal): return str(value)
    if isinstance(value,bytes): return value.hex()
    raise TypeError("Unsupported report type")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def check_wheel(path,module,version):
    assert importlib.metadata.version("anhui-data-foundation" if module == "data_foundation" else "shumei-intelligent-analysis") == version
    root = Path(importlib.import_module(module).__file__).resolve().parent
    assert "site-packages" in root.parts and root.is_relative_to(Path(sys.prefix))
    count=0
    with ZipFile(path) as archive:
        for name in archive.namelist():
            if name.startswith(module+"/") and name.endswith((".py",".json")):
                assert (root/Path(name).relative_to(module)).read_bytes()==archive.read(name)
                count+=1
    return {"version":version,"wheel_sha256":digest(path.read_bytes()),"matching_package_files":count,"origin":"isolated_site_packages"}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest",type=Path,required=True)
    parser.add_argument("--migration-config",type=Path,required=True)
    parser.add_argument("--c-wheel",type=Path,required=True)
    parser.add_argument("--e-wheel",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    url=os.environ.get("C_READINESS_DATABASE_URL","")
    parsed=make_url(url)
    if parsed.drivername!="mysql+pymysql" or not re.fullmatch(r"cde_readiness_[a-z0-9_]+_test",parsed.database or ""):
        parser.error("Use an exclusive cde_readiness_*_test MySQL schema")
    if args.output.exists(): parser.error("Use a new report/data directory")
    engine=create_engine(url)
    try:
        assert not inspect(engine).get_table_names() and not inspect(engine).get_view_names(),"Schema must be empty; nothing is removed"
    finally: engine.dispose()
    packages={"C":check_wheel(args.c_wheel,"data_foundation","0.2.1a1"),"E":check_wheel(args.e_wheel,"intelligent_analysis","0.6.0")}
    args.output.mkdir(parents=True)
    def migrate(target):
        result=subprocess.run([sys.executable,"-I","-m","alembic","-c",str(args.migration_config.resolve()),"upgrade",target],
            env={**os.environ,"DATABASE_URL":url},capture_output=True,text=True,timeout=120)
        assert result.returncode==0,"Migration failed; DB details excluded from report"
    migrate("0006")
    store=create_store(url,data_root=args.output/"raw")
    checks=[]
    def passed(name,**details): checks.append({"name":name,"status":"passed",**details})
    try:
        seed_catalog(store)
        first=import_real_dataset(store,args.manifest)
        def table_hashes():
            metadata=MetaData()
            names=[n for n in inspect(store.engine).get_table_names() if n!="alembic_version" and not n.startswith("visible_")]
            metadata.reflect(store.engine,only=names)
            result={}
            with store.engine.connect() as conn:
                for name,table in metadata.tables.items():
                    rows=[dict(r._mapping) for r in conn.execute(select(table).order_by(*table.primary_key.columns))]
                    result[name]={"rows":len(rows),"sha256":digest(json.dumps(rows,ensure_ascii=False,sort_keys=True,default=encode).encode())}
            return result
        before=table_hashes()
        assert len(before)==31
        migrate("head")
        assert table_hashes()==before
        passed("0006_to_0007_all_31_existing_tables_and_rows_unchanged",tables=before)
        repeat=import_real_dataset(store,args.manifest)
        assert first["counts"]["imported"]==83 and repeat["counts"]["skipped"]==83
        assert repeat["counts"]["imported"]==repeat["counts"]["revised"]==repeat["counts"]["quarantined"]==0
        passed("real_C_dataset_idempotent",first=first["counts"],repeat=repeat["counts"])
        manifest=json.loads(args.manifest.read_text())
        originals=[]
        for item in manifest["documents"]:
            ref=store.get_document_file_ref(item["id"])
            assert ref["document_version"]==item["document_version"] and digest(Path(ref["file_path"]).read_bytes())==ref["sha256"]
            originals.append({k:ref[k] for k in ("document_id","document_version","asset_id","source_id","sha256","byte_size")})
        passed("16_registered_original_documents_verified",documents=originals)
        spec=importlib.util.spec_from_file_location("c_metric_example",Path(__file__).with_name("c_readonly_metric_tool.py"))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        airport="28380b0b-e209-5ced-8d09-58854248f26e"
        query={"metric":"airport_throughput","entity_type":"station","entity_ids":[airport],"grain":"month",
            "period_start":"2025-01-01T00:00:00+08:00","period_end":"2025-02-01T00:00:00+08:00"}
        tool=module.build_metric_tool(store,authorized_entity_ids=[airport])
        answer=answer_message("2025年1月合肥新桥机场旅客吞吐量",context={"metric_query":query,"metric_tool":tool})
        assert Decimal(answer["metric_result"]["value"])==1111644 and answer["metric_result"]["source_ids"]
        assert tool({**query,"grain":"day"})["reason"]=="unsupported_grain"
        assert tool({**query,"period_start":"2025-12-01T00:00:00+08:00","period_end":"2026-01-01T00:00:00+08:00"})["reason"]=="data_insufficient"
        passed("real_historical_airport_metric_E_service_and_missing_periods",answer=answer,HTTP_executed=False)
        store.dispose()
        store=create_store(url,data_root=args.output/"raw")
        assert Decimal(module.build_metric_tool(store,authorized_entity_ids=[airport])(query)["value"])==1111644
        passed("new_store_connection_reads_same_historical_value")

        doc_id="0c8e2020-2e0a-5842-bce9-d9193cf81095"
        ref=store.get_document_file_ref(doc_id)
        with store.read() as uow: metadata=uow.knowledge.get_document(doc_id)
        metadata=json.loads(json.dumps(metadata,default=encode))
        counter=0
        def prepare(mutation=None):
            nonlocal counter
            counter+=1
            version="c021-acceptance-"+str(counter)
            payload={"document_id":doc_id,"document_version":ref["document_version"],"version":version,
                "file_path":ref["file_path"],"metadata":metadata}
            now=datetime.now(timezone.utc)
            with store.write() as uow:
                job=uow.jobs.enqueue_or_get(JobCreate("knowledge_index",version,payload,"C021 isolated acceptance"),now)
            with store.write() as uow:
                lease=JobLease(**uow.jobs.claim_job("c021-receiving",now,600,job_id=job["id"])["lease"])
            prepared=process_knowledge_job(payload)  # Claim transaction already closed.
            raw=prepared["artifact_bytes"]
            if mutation:
                content=json.loads(raw);item=content["documents"][doc_id]
                if mutation=="text": item["chunks"][0]["text"]+=" SYNTHETIC CORRUPTION"
                elif mutation=="locator": item["chunks"][0]["chunk_id"]="wrong-chunk-id"
                elif mutation=="metadata": item["metadata"]["title"]="wrong-title"
                else: item[mutation]="wrong-version"
                raw=json.dumps(content,ensure_ascii=False,sort_keys=True).encode()
            staged=store.persist_file(source_id=ref["source_id"],original_name="index.json",stream=BytesIO(raw))
            with store.write() as uow:
                index=uow.knowledge.begin_index(document_id=doc_id,version=version,job_id=job["id"],
                    parser_version=prepared["parser_version"],embedding_model_version=prepared["embedding_model_version"],lease=lease)
            for start in range(0,len(prepared["chunks"]),1000):
                with store.write() as uow: uow.knowledge.write_chunks(index["id"],prepared["chunks"][start:start+1000],lease)
            return prepared,staged,index,lease
        def publish(candidate):
            prepared,staged,index,lease=candidate
            with store.write() as uow:
                uow.knowledge.activate_index(document_id=doc_id,version=prepared["index_version"],artifact_path=staged["relative_path"],
                    artifact_sha256=staged["sha256"],expected_artifact_byte_size=staged["byte_size"],expected_chunk_count=len(prepared["chunks"]),lease=lease)
                uow.jobs.complete_job(lease,{"index_id":index["id"]},datetime.now(timezone.utc))
        def load(): return load_verified_knowledge_snapshot(store,[doc_id],store.data_root)
        for mutation in ("text","locator","document_version","index_version","metadata"):
            publish(prepare(mutation))
            try: load()
            except DependencyUnavailable: passed("correct_sha_rejects_wrong_"+mutation)
            else: raise AssertionError("Semantic mismatch accepted")
        baseline=prepare();publish(baseline)
        candidate=prepare()
        original=JobRepository.complete_job
        def fail(self,*a,**kw):
            original(self,*a,**kw)
            raise RuntimeError("failure after complete_job")
        try:
            with patch.object(JobRepository,"complete_job",fail): publish(candidate)
        except RuntimeError: pass
        else: raise AssertionError("Failure injection did not run")
        with store.read() as uow:
            assert uow.knowledge.get_document(doc_id)["active_index_version"]==baseline[0]["index_version"]
            assert uow.jobs.get_job(candidate[3].job_id)["status"]=="running"
        passed("C_knowledge_activation_and_post_completion_failure_rollback")
        publish(candidate)
        configure_services(ServiceBindings(knowledge_snapshot_loader=load))
        historical=answer_message("2025年春节黄山索道运营时间",context={"as_of":"2025-01-30T12:00:00+08:00"})
        current=answer_message("现在黄山索道运营时间")
        assert historical["citations"] and "历史资料" in current["answer"]
        passed("historical_and_current_notice_service_answers",historical=historical,current=current)
        with store.read() as uow:
            final_ref=uow.knowledge.get_index_artifact_ref(doc_id)
            mysql=uow.session.scalar(text("SELECT VERSION()"))
            head=uow.session.scalar(text("SELECT version_num FROM alembic_version"))
        assert head=="0007" and len(inspect(store.engine).get_table_names())-1==34
        report={"status":"passed","scope":"C_E_official_wheels_not_D_HTTP_or_real_video","packages":packages,
            "python":sys.version.split()[0],"mysql_version":mysql,"migration_head":head,"business_tables":34,
            "checks":checks,"real_document_id":doc_id,"final_index_id":final_ref["index_id"],
            "final_index_version":final_ref["version"],"final_job_id":candidate[3].job_id,
            "D_runtime_executed":False,"HTTP_executed":False,"Windows_executed":False,"real_video_inference":False,
            "crawler_touched":False,"production_database_touched":False}
        serialized=json.dumps(report,ensure_ascii=False,indent=2,default=encode)+"\n"
        assert "lease_token" not in serialized and str(args.output.resolve()) not in serialized
        (args.output/"report.json").write_text(serialized,encoding="utf-8")
        print(json.dumps({"status":"passed","checks":len(checks),"migration":head,"business_tables":34}))
    finally: store.dispose()


if __name__=="__main__":
    try: main()
    except Exception as exc:
        print(json.dumps({"status":"failed","error_type":type(exc).__name__,"details":"Inspect isolated inputs; connection configuration omitted"}),file=sys.stderr)
        raise SystemExit(1)
