from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import calendar
from uuid import uuid4

import pytest
from sqlalchemy import select

from data_foundation import AssetCreate, Conflict, InvalidArgument, MetricEntityMismatch, ObservationInput, SeriesQuery, SourceCreate, UnsupportedGrain
from data_foundation.db.models import SeriesPolicy
from conftest import SCENIC_ID, SOURCE_ID


def add_source(store,grain="month"):
    key=str(uuid4())
    with store.write() as uow:uow.provenance.register_source(SourceCreate(key,"合成来源","合成序列","https://example.invalid/"+key,"仅合成存储验收","irregular",native_grain=grain,verification_status="verified"))
    return key


def add_point(store,start,end=None,*,source_id=SOURCE_ID,grain="month",value="10",method="synthetic-v1",quality="verified"):
    end=end or (date(start.year,start.month,calendar.monthrange(start.year,start.month)[1]) if grain=="month" else start)
    now=datetime.now(timezone.utc)
    with store.write() as uow:
        asset=uow.provenance.register_asset(AssetCreate(source_id,"https://example.invalid/"+uuid4().hex,"合成数据",now,"test-only",uuid4().hex*2,"text/csv",0,"仅合成验收"))
        run=uow.observations.start_run(source_id=source_id,asset_id=asset['id'],dry_run=False,reason="test")
        result=uow.observations.add_or_revise(ObservationInput(SCENIC_ID,"scenic_visits",start,end,grain,Decimal(value),"人次",value,"人次",Decimal("1"),source_id,asset['id'],"test row",method,published_at=now,quality_status=quality),run.id)
        uow.observations.finish_run(run.id,counts={'processed':1,'imported':1})
        return result[1]


def policy(uow,source_id=SOURCE_ID,**patch):
    return uow.observations.set_series_policy(**{'entity_id':SCENIC_ID,'metric_code':'scenic_visits','grain':'month',
        'valid_from':date(2026,1,1),'valid_until':date(2026,12,31),'source_id':source_id,'reason':'合成核验指定来源','policy_version':'test-v1',**patch})


def test_coverage_segments_gaps_methodology_and_cutoff(store):
    for month in (1,2,4,5):add_point(store,date(2026,month,1),method='synthetic-v2' if month>=4 else 'synthetic-v1')
    with store.read() as uow:
        result=uow.observations.get_coverage(SeriesQuery(SCENIC_ID,'scenic_visits','month',date(2026,1,1),date(2026,6,30)))
        assert result['expected_period_count']==6 and result['available_period_count']==4
        assert result['coverage_ratio']==Decimal(4)/6
        assert result['longest_contiguous_period_count']==result['longest_comparable_period_count']==2
        assert result['max_gap_period_count']==1 and len(result['missing_segments'])==2
        assert len(result['methodology_breaks'])==1
        assert result['latest_ingested_at'].tzinfo is not None
        latest=uow.observations.get_latest_verified(SCENIC_ID,'scenic_visits','month',date(2026,5,15))
        assert latest['period_start']==date(2026,4,1)


def test_source_conflicts_are_missing_and_latest_skips_conflict(store):
    other=add_source(store)
    add_point(store,date(2026,1,1))
    add_point(store,date(2026,2,1))
    add_point(store,date(2026,2,1),source_id=other,value='11')
    with store.read() as uow:
        result=uow.observations.get_coverage(SeriesQuery(SCENIC_ID,'scenic_visits','month',date(2026,1,1),date(2026,2,28)))
        assert result['available_period_count']==1 and result['missing_periods'][0]['period_start']==date(2026,2,1)
        assert result['diagnostics'][0]['code']=='source_conflict'
        assert uow.observations.get_latest_verified(SCENIC_ID,'scenic_visits','month',date(2026,3,1))['period_start']==date(2026,1,1)


def test_policy_selects_source_and_does_not_fallback_when_preferred_missing(store):
    other=add_source(store)
    add_point(store,date(2026,1,1))
    add_point(store,date(2026,1,1),source_id=other,value='11')
    add_point(store,date(2026,2,1),source_id=other)
    with store.write() as uow:
        first=policy(uow)
        assert policy(uow)['id']==first['id']
    with store.read() as uow:
        result=uow.observations.query_series(SeriesQuery(SCENIC_ID,'scenic_visits','month',date(2026,1,1),date(2026,2,28)))
        assert len(result['points'])==1 and result['points'][0]['value']==Decimal('10')
        assert result['diagnostics'][0]['code']=='preferred_source_missing'


def test_declared_support_without_observations_is_empty_not_unsupported(store):
    with store.write() as uow:policy(uow)
    with store.read() as uow:
        result=uow.observations.get_coverage(SeriesQuery(SCENIC_ID,'scenic_visits','month',date(2026,1,1),date(2026,2,28)))
        assert result['available_period_count']==0 and result['expected_period_count']==2 and result['max_gap_period_count']==2
        item=next(x for x in uow.catalog.list_entities()['items'] if x['id']==SCENIC_ID)
        assert item['available_metrics']==[] and item['supported_metrics']==['scenic_visits']
        assert item['supported_grains']==['month']
        with pytest.raises(UnsupportedGrain):uow.observations.query_series(SeriesQuery(SCENIC_ID,'scenic_visits','day',date(2026,1,1),date(2026,1,2)))


def test_pending_and_noncanonical_months_do_not_count_as_available(store):
    add_point(store,date(2026,1,1),quality='pending_review')
    add_point(store,date(2026,2,2),date(2026,2,28))
    with store.read() as uow:
        result=uow.observations.get_coverage(SeriesQuery(SCENIC_ID,'scenic_visits','month',date(2026,1,1),date(2026,2,28)))
        assert result['available_period_count']==0
        assert result['diagnostics'][0]['code']=='noncanonical_period_excluded'
        assert uow.observations.get_latest_verified(SCENIC_ID,'scenic_visits','month',date(2026,3,1)) is None


@pytest.mark.parametrize('patch',[{'reason':''},{'policy_version':'x'*33},{'valid_from':datetime(2026,1,1)},{'valid_until':date(2025,1,1)},{'grain':'invalid'}])
def test_invalid_policy(store,patch):
    with store.write() as uow:
        with pytest.raises(InvalidArgument):policy(uow,**patch)


def test_policy_overlap_different_content_and_native_grain_rejected(store):
    wrong=add_source(store,'day')
    with store.write() as uow:
        policy(uow)
        with pytest.raises(Conflict):policy(uow,reason='different')
        with pytest.raises(InvalidArgument):policy(uow,source_id=wrong)


def test_policy_requires_write_and_rolls_back(store):
    with store.read() as uow:
        with pytest.raises(InvalidArgument):policy(uow)
    with pytest.raises(RuntimeError):
        with store.write() as uow:
            policy(uow)
            raise RuntimeError('rollback')
    with store.read() as uow:assert uow.session.scalars(select(SeriesPolicy).where(SeriesPolicy.entity_id==SCENIC_ID)).all()==[]


def test_partial_edges_and_empty_coverage(store):
    with store.read() as uow:
        result=uow.observations.get_coverage(SeriesQuery(SCENIC_ID,'scenic_visits','month',date(2026,1,15),date(2026,2,14)))
        assert result['expected_period_count']==0 and result['available_period_count']==0
        assert result['longest_contiguous_period_count']==result['max_gap_period_count']==0
        assert len(result['diagnostics'])==2
        with pytest.raises(InvalidArgument):uow.observations.get_coverage(SeriesQuery(SCENIC_ID,'scenic_visits','month',date(2026,1,1),date(2026,2,28),quality='all'))


@pytest.mark.parametrize('as_of',[None,'2026-01-01',True,datetime(2026,1,1)])
def test_latest_invalid_cutoff(store,as_of):
    with store.read() as uow:
        with pytest.raises(InvalidArgument):uow.observations.get_latest_verified(SCENIC_ID,'scenic_visits','month',as_of)


def create_holiday(store,*,region='000000',scope='CN:national_general_public'):
    now=datetime.now(timezone.utc)
    with store.write() as uow:
        asset=uow.provenance.register_asset(AssetCreate(SOURCE_ID,'https://example.invalid/'+uuid4().hex,'合成假期定义',now,'test-only',uuid4().hex*2,'text/plain',0,'仅合成验收'))
        return uow.holidays.create_holiday({'name':'合成五一','kind':'may_day','year':2026,'start_date':date(2026,5,1),'end_date':date(2026,5,3),
            'days_count':3,'region_code':region,'scope_key':scope,'definition_source_id':SOURCE_ID,'definition_asset_id':asset['id']})


def test_holiday_complete_daily_sum_and_exact_original_period(store):
    holiday=create_holiday(store)
    daily=add_source(store,'day');exact=add_source(store,'holiday')
    for day in (1,2,3):add_point(store,date(2026,5,day),grain='day',source_id=daily,value=str(day))
    add_point(store,date(2026,5,1),date(2026,5,3),grain='holiday',source_id=exact,value='6')
    with store.read() as uow:
        result=uow.observations.query_holiday_values(SCENIC_ID,'scenic_visits',holiday['id'])
        assert result['daily_sum']==Decimal('6') and result['exact_total']['value']==Decimal('6')
        assert result['daily_average']==Decimal('2') and result['day_index_supported']
        assert not result['scope_confirmation_required'] and not result['diagnostics']


def test_holiday_monthly_value_is_not_split_or_zero_filled(store):
    holiday=create_holiday(store)
    add_point(store,date(2026,5,1),value='100')
    with store.read() as uow:
        result=uow.observations.query_holiday_values(SCENIC_ID,'scenic_visits',holiday['id'])
        assert result['exact_total'] is result['daily_sum'] is result['daily_average'] is None
        assert len(result['missing_days'])==3 and not result['day_index_supported'] and not result['daily_grain_supported']


def test_holiday_partial_daily_data_and_scope_region_confirmation(store):
    holiday=create_holiday(store,region='340000',scope='school:synthetic')
    source=add_source(store,'day')
    add_point(store,date(2026,5,1),grain='day',source_id=source)
    with store.read() as uow:
        result=uow.observations.query_holiday_values(SCENIC_ID,'scenic_visits',holiday['id'])
        assert result['scope_confirmation_required'] and len(result['missing_days'])==2 and result['daily_sum'] is None
    other=create_holiday(store,region='310000')
    with store.read() as uow:
        with pytest.raises(InvalidArgument):uow.observations.query_holiday_values(SCENIC_ID,'scenic_visits',other['id'])


def test_holiday_total_conflict_and_daily_methodology_break_not_hidden(store):
    holiday=create_holiday(store)
    source=add_source(store,'day');exact=add_source(store,'holiday')
    for day in (1,2,3):add_point(store,date(2026,5,day),grain='day',source_id=source,value='1')
    add_point(store,date(2026,5,1),date(2026,5,3),grain='holiday',source_id=exact,value='9')
    with store.read() as uow:
        result=uow.observations.query_holiday_values(SCENIC_ID,'scenic_visits',holiday['id'])
        assert result['daily_sum']==Decimal('3') and result['exact_total']['value']==Decimal('9')
        assert result['diagnostics'][0]['code']=='holiday_total_daily_sum_mismatch'
    add_point(store,date(2026,5,3),grain='day',source_id=source,value='1',method='synthetic-v2')
    with store.read() as uow:
        result=uow.observations.query_holiday_values(SCENIC_ID,'scenic_visits',holiday['id'])
        assert result['daily_sum'] is None and not result['day_index_supported']
        assert result['diagnostics'][0]['code']=='daily_methodology_break'


def test_all_quality_keeps_raw_conflicts_but_does_not_mark_coverage_verified(store):
    other=add_source(store)
    add_point(store,date(2026,1,1))
    add_point(store,date(2026,1,1),source_id=other,value='11')
    with store.read() as uow:
        series=uow.observations.query_series(SeriesQuery(SCENIC_ID,'scenic_visits','month',date(2026,1,1),date(2026,1,31),quality='all'))
        assert len(series['points'])==2 and series['coverage_ratio']==0
        assert series['diagnostics'][0]['code']=='source_conflict'
