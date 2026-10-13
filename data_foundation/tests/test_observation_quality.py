from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from data_foundation import AssetCreate, Conflict, InvalidArgument, ObservationInput, SourceCreate
from data_foundation.db.models import Observation, ObservationRevision
from conftest import SCENIC_ID, SOURCE_ID


def input_data(store, source_id=SOURCE_ID):
    now=datetime.now(timezone.utc)
    with store.write() as uow:
        asset=uow.provenance.register_asset(AssetCreate(source_id,'https://example.invalid/'+uuid4().hex,'合成数据',now,'test-only',uuid4().hex*2,'text/csv',0,'仅合成验收'))
        run=uow.observations.start_run(source_id=source_id,asset_id=asset['id'],dry_run=False,reason='test')
    return run.id,ObservationInput(SCENIC_ID,'scenic_visits',date(2026,1,1),date(2026,1,31),'month',Decimal('10000'),'人次','1','万人次',Decimal('10000'),source_id,asset['id'],'test row','synthetic-v1',published_at=now)


@pytest.mark.parametrize('patch',[
    {'value':Decimal('NaN')},{'value':Decimal('Infinity')},{'value':Decimal('-1')},{'value':Decimal('10000.00001')},{'value':Decimal('10000000000000000')},{'value':True},{'value':10000.0},
    {'conversion_factor':Decimal('NaN')},{'conversion_factor':Decimal('Infinity')},{'conversion_factor':Decimal('0')},{'conversion_factor':Decimal('-1')},{'conversion_factor':1.0},
    {'original_value':'预计1'},{'original_value':'NaN'},{'original_value':'-1'},{'original_value':'2'},{'original_unit':'未知单位'},
    {'source_locator':''},{'source_locator':'x'*501},{'methodology_version':' '},{'published_at':datetime(2026,1,1)},
    {'period_start':datetime(2026,1,1)},{'period_end':date(2025,12,31)},{'period_start':date(2026,1,2)},
    {'grain':'day'},{'grain':'week'},{'quality_status':[]},
])
def test_bad_observation_rejected_before_insert(store,patch):
    run_id,data=input_data(store)
    with store.write() as uow:
        with pytest.raises(InvalidArgument):uow.observations.add_or_revise(replace(data,**patch),run_id)
        assert uow.session.scalars(select(Observation).where(Observation.entity_id==SCENIC_ID)).all()==[]


def test_known_unit_factor_and_value_must_both_match(store):
    run_id,data=input_data(store)
    with store.write() as uow:
        with pytest.raises(InvalidArgument):uow.observations.add_or_revise(replace(data,value=Decimal('1'),conversion_factor=Decimal('1')),run_id)
        assert uow.observations.add_or_revise(data,run_id)[0]=='imported'
        assert uow.observations.add_or_revise(data,run_id)[0]=='skipped'


def test_complete_week_day_and_leap_month_boundaries(store):
    for start,end,grain in [(date(2024,2,1),date(2024,2,29),'month'),(date(2026,1,5),date(2026,1,11),'week'),(date(2026,1,12),date(2026,1,12),'day')]:
        source_id=str(uuid4())
        with store.write() as uow:
            uow.provenance.register_source(SourceCreate(source_id,"合成","合成周期来源","https://example.invalid/"+source_id,"仅合成","irregular",native_grain=grain))
        run_id,data=input_data(store,source_id)
        with store.write() as uow:
            assert uow.observations.add_or_revise(replace(data,period_start=start,period_end=end,grain=grain),run_id)[0]=='imported'
    with store.write() as uow:
        with pytest.raises(InvalidArgument):uow.observations.add_or_revise(replace(data,period_start=date(2026,1,6),period_end=date(2026,1,12),grain='week'),run_id)


def test_monthly_source_cannot_be_declared_as_daily_observation(store):
    run_id,data=input_data(store)
    with store.write() as uow:
        with pytest.raises(InvalidArgument):uow.observations.add_or_revise(replace(data,period_end=data.period_start,grain='day'),run_id)


def test_finished_run_cannot_write_and_revisions_preserve_prior_values(store):
    run_id,data=input_data(store)
    with store.write() as uow:
        uow.observations.add_or_revise(data,run_id)
        uow.observations.finish_run(run_id,counts={'processed':1,'imported':1})
        with pytest.raises(Conflict):uow.observations.add_or_revise(data,run_id)
        run=uow.observations.start_run(source_id=SOURCE_ID,asset_id=data.asset_id,dry_run=False,reason='revision')
        action,current=uow.observations.add_or_revise(replace(data,original_value='2',value=Decimal('20000')),run.id)
        assert action=='revised' and current['row_version']==2
        history=uow.session.scalar(select(ObservationRevision).where(ObservationRevision.observation_id==current['id']))
        assert history.snapshot['value']=='10000.0000' and history.version==1


def test_dry_run_rollbacks_and_read_only(store):
    run_id,data=input_data(store)
    with store.read() as uow:
        with pytest.raises(InvalidArgument):uow.observations.add_or_revise(data,run_id)
    with store.write() as uow:
        assert uow.observations.add_or_revise(data,run_id,dry_run=True)==('imported',None)
    with pytest.raises(RuntimeError):
        with store.write() as uow:
            uow.observations.add_or_revise(data,run_id)
            raise RuntimeError('rollback')
    with store.read() as uow:assert uow.session.scalars(select(Observation).where(Observation.entity_id==SCENIC_ID)).all()==[]
