"""空独占MySQL库：人工标注并发、批次回滚和计数聚合验收。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, datetime, timezone
import argparse
import json
import os
from pathlib import Path
import sys
from threading import Event

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from data_foundation import Conflict, CountInput, __version__, create_store
from data_foundation.db.models import ManualAnnotation, Video
from de_handoff_smoke import migrate
from worker_lifecycle_fixture import make_fixture


def verify(url,data_root):
    parsed=make_url(url)
    if parsed.drivername!='mysql+pymysql' or not (parsed.database or '').endswith('_test'):
        raise ValueError('仅允许名称以_test结尾的独占MySQL测试库')
    engine=create_engine(url)
    try:
        if inspect(engine).get_table_names():raise ValueError('库必须为空；不删除已有表')
    finally:engine.dispose()
    migrate(url)
    store=create_store(url,data_root=data_root)
    checks=[]
    def passed(name):checks.append({'name':name,'status':'passed'})
    def config(uow):
        uow.session.execute(text('SET SESSION innodb_lock_wait_timeout = 5'))
        return uow.session.scalar(text('SELECT CONNECTION_ID()'))
    try:
        f=make_fixture(store)
        item={'video_id':f['video']['id'],'camera_id':f['camera_id'],'sample_key':'synthetic-sample','version':1,
            'window_start':f['now'],'window_end':f['now']+timedelta(minutes=5),'entries':10,'exits':3,
            'annotator':'synthetic','direction_note':'仅测试方向','review_status':'verified'}
        with ThreadPoolExecutor(max_workers=1) as pool:
            began=Event()
            def second_annotation():
                with store.write() as uow:
                    connection=config(uow);began.set()
                    return connection,uow.video.write_manual_annotations([item])
            with store.write() as first:
                first_id=config(first)
                assert first.video.write_manual_annotations([item])=={'inserted':1,'skipped':0}
                future=pool.submit(second_annotation);assert began.wait(3)
            second_id,result=future.result(timeout=6)
            assert second_id!=first_id and result=={'inserted':0,'skipped':1}
        passed('two_annotators_same_version_content_insert_once')
        with ThreadPoolExecutor(max_workers=1) as pool:
            began=Event()
            def conflicting_revision():
                with store.write() as uow:
                    config(uow);began.set()
                    try:uow.video.write_manual_annotations([{**item,'version':2,'entries':12}])
                    except Conflict:return 'conflict'
                    return 'unexpected'
            with store.write() as first:
                first.video.write_manual_annotations([{**item,'version':2,'entries':11,'review_status':'rejected'}])
                future=pool.submit(conflicting_revision);assert began.wait(3)
            assert future.result(timeout=6)=='conflict'
        with store.read() as uow:
            assert uow.video.list_manual_annotations(f['video']['id'])['total']==1
            assert uow.video.list_manual_annotations(f['video']['id'],review_status='verified')['total']==0
            assert uow.video.list_manual_annotations(f['video']['id'],latest_only=False)['total']==2
        passed('conflicting_revision_serializes_and_latest_filter_does_not_resurrect_old_review')
        # 先读空样本快照，另一事务发布版本1，后续写版本2必须当前读。
        with ThreadPoolExecutor(max_workers=1) as pool:
            cached,go=Event(),Event()
            def stale_revision():
                with store.write() as uow:
                    config(uow)
                    assert uow.session.scalars(select(ManualAnnotation).where(ManualAnnotation.video_id==f['video']['id'],ManualAnnotation.sample_key=='another')).all()==[]
                    cached.set();assert go.wait(3)
                    return uow.video.write_manual_annotations([{**item,'sample_key':'another','version':2,'entries':12}])
            future=pool.submit(stale_revision);assert cached.wait(3)
            with store.write() as writer:
                writer.video.write_manual_annotations([{**item,'sample_key':'another'}]);go.set()
            assert future.result(timeout=6)=={'inserted':1,'skipped':0}
        passed('annotation_versions_use_current_read_after_stale_snapshot')
        with store.write() as uow:
            try:uow.video.write_manual_annotations([{**item,'sample_key':'rolled-back'},{**item,'entries':999}])
            except Conflict:pass
            else:raise AssertionError('expected conflict')
            assert uow.video.list_manual_annotations(f['video']['id'])['total']==2
        passed('batch_conflict_rollback_retains_preexisting_annotations')
        try:
            with store.write() as uow:
                uow.video.write_manual_annotations([{**item,'sample_key':'outer-rollback'}])
                raise RuntimeError('rollback')
        except RuntimeError:pass
        with store.read() as uow:assert uow.video.list_manual_annotations(f['video']['id'])['total']==2
        passed('outer_transaction_rollback_does_not_commit_savepoint_annotations')
        with store.write() as uow:
            uow.session.get(Video,f['video']['id']).recorded_end_at=(f['now']+timedelta(minutes=30)).replace(tzinfo=None)
            points=[CountInput(f['now']+timedelta(minutes=5*i),f['now']+timedelta(minutes=5*(i+1)),i+1,1) for i in (0,1,2,3,5)]
            uow.video.write_counts(f['analysis']['id'],points,f['video_lease'])
            uow.video.complete_analysis(f['analysis']['id'],len(points),f['video_lease'])
        with store.read() as uow:
            result=uow.video.query_counts(f['video']['id'],f['analysis']['id'],interval_minutes=15)
            assert len(result['points'])==1 and result['points'][0]['entries']==6
            assert result['aggregation']['missing_windows'][0]['available_windows']==2
        passed('aggregation_omits_incomplete_window_instead_of_filling_zero')
        with store.read() as uow:version=uow.session.scalar(text('SELECT VERSION()'))
        return {'status':'passed','package_version':__version__,'migration_head':'0006','mysql_version':version,'check_count':len(checks),'checks':checks,
            'verified_at':datetime.now(timezone.utc).isoformat(),'scope':'isolated_synthetic_storage_only','production_database_verified':False}
    finally:store.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--data-root',type=Path,required=True)
    args=parser.parse_args()
    report=verify(os.environ['TEST_DATABASE_URL'],args.data_root)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'checks':report['check_count']}))
