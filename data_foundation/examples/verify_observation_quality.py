"""空独占MySQL库：同来源重复/修订竞争、回滚及真实快照回归。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import argparse
import json
import os
from pathlib import Path
import sys
from threading import Event
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from data_foundation import AssetCreate, ObservationInput, SeriesQuery, __version__, create_store
from data_foundation.db.models import Observation, ObservationRevision
from data_foundation.ingestion.real_dataset import import_real_dataset
from de_handoff_smoke import migrate
from worker_lifecycle_fixture import make_fixture


def verify(url,data_root,manifest):
    parsed=make_url(url)
    if parsed.drivername!='mysql+pymysql' or not (parsed.database or '').endswith('_test'):raise ValueError('仅_test独占MySQL库')
    engine=create_engine(url)
    try:
        if inspect(engine).get_table_names():raise ValueError('库必须为空，不删除已有数据')
    finally:engine.dispose()
    migrate(url);store=create_store(url,data_root=data_root);checks=[]
    def passed(name,**details):checks.append({'name':name,'status':'passed',**details})
    try:
        imported=import_real_dataset(store,manifest)
        f=make_fixture(store);source_id=f['asset']['source_id']
        # 合成Source的native_grain为None，不声明原生粒度。
        with store.write() as uow:
            asset=uow.provenance.register_asset(AssetCreate(source_id,'https://example.invalid/'+uuid4().hex,'合成观测',f['now'],'test-only',uuid4().hex*2,'text/csv',0,'仅合成'))
            def make_run():return uow.observations.start_run(source_id=source_id,asset_id=asset['id'],dry_run=False,reason='synthetic').id
            runs=[make_run() for _ in range(6)]
        data=ObservationInput(f['scenic_id'],'scenic_visits',date(2026,1,1),date(2026,1,31),'month',Decimal('10'),'人次','10','人次',Decimal(1),source_id,asset['id'],'synthetic row','synthetic-v1',published_at=f['now'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            began=Event()
            def second_insert():
                with store.write() as uow:
                    uow.session.execute(text('SET SESSION innodb_lock_wait_timeout = 5'));began.set()
                    return uow.observations.add_or_revise(data,runs[1])[0]
            with store.write() as first:
                action,row=first.observations.add_or_revise(data,runs[0]);assert action=='imported'
                future=pool.submit(second_insert);assert began.wait(3)
            assert future.result(timeout=6)=='skipped'
        passed('same_source_concurrent_insert_and_duplicate_produce_one_observation')
        with ThreadPoolExecutor(max_workers=1) as pool:
            cached,go=Event(),Event()
            def stale_revision():
                with store.write() as uow:
                    uow.session.execute(text('SET SESSION innodb_lock_wait_timeout = 5'))
                    cached_row=uow.session.get(Observation,row['id']);assert cached_row.row_version==1
                    cached.set();assert go.wait(3)
                    return uow.observations.add_or_revise(replace(data,value=Decimal('30'),original_value='30'),runs[3])
            future=pool.submit(stale_revision);assert cached.wait(3)
            with store.write() as first:
                action,_=first.observations.add_or_revise(replace(data,value=Decimal('20'),original_value='20'),runs[2]);assert action=='revised'
                go.set()
            action,current=future.result(timeout=6);assert action=='revised' and current['row_version']==3
        with store.read() as uow:
            history=uow.session.scalars(select(ObservationRevision).where(ObservationRevision.observation_id==row['id']).order_by(ObservationRevision.version)).all()
            assert [entry.version for entry in history]==[1,2]
            assert [entry.snapshot['value'] for entry in history]==['10.0000','20.0000']
            assert uow.session.get(Observation,row['id']).value==Decimal('30')
        passed('cached_concurrent_revisions_preserve_both_history_versions')
        try:
            with store.write() as uow:
                uow.observations.add_or_revise(replace(data,value=Decimal('40'),original_value='40'),runs[4])
                raise RuntimeError('rollback')
        except RuntimeError:pass
        with store.read() as uow:
            assert uow.session.get(Observation,row['id']).value==Decimal('30')
            assert len(uow.session.scalars(select(ObservationRevision).where(ObservationRevision.observation_id==row['id'])).all())==2
        passed('failed_revision_transaction_rolls_back_value_and_history')
        with store.write() as uow:
            action,preview=uow.observations.add_or_revise(replace(data,value=Decimal('40'),original_value='40'),runs[5],dry_run=True)
            assert action=='revised' and preview['value']==Decimal('30') and preview['row_version']==3
        with store.read() as uow:assert uow.session.get(Observation,row['id']).row_version==3
        passed('dry_run_revision_does_not_change_value_version_or_history')
        again=import_real_dataset(store,manifest)
        assert imported['counts']['imported']==83 and again['counts']['imported']==again['counts']['revised']==0 and again['counts']['skipped']==83
        passed('strict_validation_keeps_all_83_real_observations_and_repeat_import')
        with store.read() as uow:mysql=uow.session.scalar(text('SELECT VERSION()'))
        return {'status':'passed','package_version':__version__,'migration_head':'0006','mysql_version':mysql,'check_count':len(checks),'checks':checks,
            'verified_at':datetime.now(timezone.utc).isoformat(),'scope':'isolated_synthetic_concurrency_and_existing_public_snapshot','production_database_verified':False}
    finally:store.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True);parser.add_argument('--data-root',type=Path,required=True);parser.add_argument('--manifest',type=Path,required=True)
    args=parser.parse_args();report=verify(os.environ['TEST_DATABASE_URL'],args.data_root,args.manifest)
    args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'checks':report['check_count']}))
