"""独占空MySQL库中的来源策略竞争及既有真实快照覆盖验收。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from threading import Event

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from data_foundation import Conflict, SeriesQuery, __version__, create_store
from data_foundation.db.models import SeriesPolicy
from data_foundation.ingestion.real_dataset import import_real_dataset
from de_handoff_smoke import migrate
from worker_lifecycle_fixture import make_fixture


def verify(url,data_root,manifest):
    parsed=make_url(url)
    if parsed.drivername!='mysql+pymysql' or not (parsed.database or '').endswith('_test'):
        raise ValueError('仅允许_test空MySQL独占库')
    engine=create_engine(url)
    try:
        if inspect(engine).get_table_names():raise ValueError('必须为空库，不删除已有数据')
    finally:engine.dispose()
    migrate(url)
    store=create_store(url,data_root=data_root)
    checks=[]
    def passed(name,**details):checks.append({'name':name,'status':'passed',**details})
    try:
        imported=import_real_dataset(store,manifest)
        data=json.loads(manifest.read_text())
        airport=next(entity for entity in data['catalog']['entities'] if entity['type']=='airport')
        source_id=next(row['source_id'] for row in data['observations'] if row['entity_id']==airport['id'])
        params={'entity_id':airport['id'],'metric_code':'airport_throughput','grain':'month','valid_from':date(2023,1,1),'valid_until':date(2026,8,31),
            'source_id':source_id,'reason':'仅合成策略验收；明确指定现有官方来源','policy_version':'synthetic-test-v1'}
        with ThreadPoolExecutor(max_workers=1) as pool:
            started=Event()
            def second_policy():
                with store.write() as uow:
                    uow.session.execute(text('SET SESSION innodb_lock_wait_timeout = 5'))
                    started.set()
                    return uow.observations.set_series_policy(**params)
            with store.write() as first:
                first_policy=first.observations.set_series_policy(**params)
                future=pool.submit(second_policy);assert started.wait(3)
            assert future.result(timeout=6)['id']==first_policy['id']
        passed('identical_concurrent_source_policies_reuse_one_record')
        with ThreadPoolExecutor(max_workers=1) as pool:
            cached,go=Event(),Event()
            def stale_policy():
                with store.write() as uow:
                    uow.session.execute(text('SET SESSION innodb_lock_wait_timeout = 5'))
                    assert uow.session.scalars(select(SeriesPolicy).where(SeriesPolicy.entity_id==airport['id'],SeriesPolicy.valid_from==date(2027,1,1))).all()==[]
                    cached.set();assert go.wait(3)
                    try:uow.observations.set_series_policy(**{**params,'valid_from':date(2027,1,1),'valid_until':date(2027,12,31),'reason':'different'})
                    except Conflict:return 'conflict'
                    return 'unexpected'
            future=pool.submit(stale_policy);assert cached.wait(3)
            with store.write() as writer:
                writer.observations.set_series_policy(**{**params,'valid_from':date(2027,1,1),'valid_until':date(2027,12,31)})
                go.set()
            assert future.result(timeout=6)=='conflict'
        passed('source_policy_overlap_is_rechecked_after_old_snapshot_and_lock_wait')
        for entity in data['catalog']['entities']:
            if entity['type']!='airport':continue
            with store.read() as uow:
                report=uow.observations.get_coverage(SeriesQuery(entity['id'],'airport_throughput','month',date(2023,1,1),date(2026,8,31)))
                assert report['expected_period_count']==44 and report['available_period_count']==41
                assert report['longest_contiguous_period_count']==11 and report['max_gap_period_count']==1
                assert report['coverage_ratio']==Decimal(41)/44
                latest=uow.observations.get_latest_verified(entity['id'],'airport_throughput','month',date(2026,7,15))
                assert latest['period_end']==date(2026,6,30)
            passed('real_airport_missing_decembers_remain_missing',entity_name=entity['name'],expected=44,available=41,longest_contiguous=11,max_gap=1)
        holiday=next(h for h in store_holidays(store) if h['kind']=='may_day' and h['year']==2025)
        with store.read() as uow:
            evidence=uow.observations.query_holiday_values(airport['id'],'airport_throughput',holiday['id'])
            assert evidence['exact_total'] is evidence['daily_sum'] is evidence['daily_average'] is None
            assert not evidence['day_index_supported']
            assert len(evidence['missing_days'])==holiday['days_count']
        passed('real_monthly_data_not_split_into_holiday_day_values')
        again=import_real_dataset(store,manifest)
        assert again['counts']['imported']==again['counts']['revised']==0 and again['counts']['skipped']==83
        passed('existing_real_snapshot_stays_idempotent_after_policy_and_coverage_queries')
        with store.read() as uow:mysql=uow.session.scalar(text('SELECT VERSION()'))
        return {'status':'passed','package_version':__version__,'migration_head':'0006','mysql_version':mysql,'check_count':len(checks),'checks':checks,
            'real_observations':83,'scope':'isolated_storage_and_existing_public_snapshot','production_database_verified':False,
            'verified_at':datetime.now(timezone.utc).isoformat()}
    finally:store.dispose()


def store_holidays(store):
    with store.read() as uow:return uow.holidays.list_holidays()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--data-root',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    args=parser.parse_args()
    report=verify(os.environ['TEST_DATABASE_URL'],args.data_root,args.manifest)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'checks':report['check_count']}))
