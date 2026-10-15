"""Snapshot compatibility and the actual service gate used by both callers."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from api.services.models import (AppUserProfile, Exercise, WorkoutSession,
    WorkoutSessionExercise, WorkoutSessionSet, PeriodReport, AppNotification)
from api.services.analytics.rebuild import rebuild_user
from api.services.analytics.state import mark_dirty
from api.services.analytics.service import exercise_analytics
from api.services.analytics.types import AnalyticsQuery
from api.services.reports import service

START, END, TODAY = date(2026, 8, 10), date(2026, 8, 16), date(2026, 8, 17)
CTX = dict(schema_version=1, model='barbell', input_basis='full')


@pytest.fixture
async def history(db, test_user, monkeypatch):
    # Keep the materializer's real period selection but bound this test to weeks.
    monkeypatch.setattr(service, 'PERIOD_TYPES', ('week',))
    db.add(AppUserProfile(app_user_id=test_user.id, timezone='Europe/Moscow'))
    exercise = Exercise(name='reports-'+str(uuid4()), category='base',
        main_muscle_group='chest', secondary_muscle_groups=['triceps'],
        difficulty='beginner', equipment_needed=[], source='custom', app_user_id=test_user.id)
    db.add(exercise)
    await db.flush()
    # Baseline, first LOCAL day, later same-period PR, and excluded next-local-week.
    for stamp, weight, reps in [('2026-08-08T10:00:00',100,1),
                               ('2026-08-09T21:30:00',80,10),
                               ('2026-08-12T10:00:00',120,1),
                               ('2026-08-16T22:00:00',999,1)]:
        finish = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
        w = WorkoutSession(app_user_id=test_user.id, source='free', status='finished',
            started_at=finish-timedelta(hours=1), finished_at=finish,
            analytics_snapshot={'is_deload':False})
        db.add(w)
        await db.flush()
        ex = WorkoutSessionExercise(workout_session_id=w.id, exercise_id=exercise.id, order_index=0)
        db.add(ex)
        await db.flush()
        db.add(WorkoutSessionSet(workout_session_exercise_id=ex.id, set_number=1,
            weight=weight, reps=reps, is_completed=True, analytics_load_snapshot=CTX))
        if reps == 10:
            db.add(WorkoutSessionSet(workout_session_exercise_id=ex.id, set_number=2,
                set_type='drop', weight=20, reps=5, is_completed=True,
                analytics_load_snapshot=dict(schema_version=1,model='dumbbell',
                    input_basis='per_implement',implement_count=2,rep_basis='simultaneous')))
    await db.commit()
    return exercise.id


async def report(db, uid):
    return (await db.execute(select(PeriodReport).where(PeriodReport.app_user_id==uid,
        PeriodReport.period_type=='week',PeriodReport.period_start==START))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_new_report_matches_analytics(db, test_user, history, client, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','true')
    ready = await rebuild_user(db,test_user.id)
    assert ready['status']=='ready'
    await service.ensure_reports(db,test_user.id,TODAY)
    await db.commit()
    row = await report(db,test_user.id)
    common = await exercise_analytics(db,test_user.id,history,AnalyticsQuery(period='custom',start=START,end=END))
    assert (row.rules_version,row.shape_version)==(3,3)
    metrics = row.payload['metrics']
    assert metrics['time']['sessions']==2
    assert metrics['volume']['work_sets']==2
    assert metrics['volume']['tonnage_kg']==1120  # 800 normal + 200 normalized drop + 120
    assert metrics['volume']['by_muscle']['chest']['direct']==3
    assert metrics['volume']['by_muscle']['triceps']['indirect']==3  # Raw, not effective 1.5.
    assert metrics['intensity']['avg_relative']==pytest.approx(common.summary.intensity.value/100)
    assert metrics['intensity']['avg_relative']==pytest.approx((8 + 120/(80*10**.1))/11)
    assert metrics['effort']==dict(avg_rir=None,labeled_share=0.0)
    assert metrics['adherence']['rate'] is None
    assert not any(a['id']=='adherence_low' for a in row.payload['actions'])
    assert max(r['value'] for r in metrics['records'])==common.summary.best_e1rm.value==120
    assert all(r['record_type']=='e1rm_lombardi_v1' for r in metrics['records'])
    assert row.payload['analytics_meta']==dict(rules_version='p1-08-v1',formula_id='lombardi-v1',data_revision=ready['source_revision'])
    response = await client.get('/reports/week/2026-08-10')
    assert response.status_code==200
    assert response.json()['analytics_meta']==row.payload['analytics_meta']
    assert 'Lombardi' in response.json()['calculation_label']
    assert await service.ensure_reports(db,test_user.id,TODAY)==0


@pytest.mark.asyncio
async def test_report_waits_for_ready_revision(db, test_user, history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','true')
    assert await service.ensure_reports(db,test_user.id,TODAY)==0
    assert await report(db,test_user.id) is None
    assert not (await db.execute(select(AppNotification).where(AppNotification.app_user_id==test_user.id))).scalars().all()
    await rebuild_user(db,test_user.id)
    await mark_dirty(db,test_user.id)
    await db.commit()
    assert await service.ensure_reports(db,test_user.id,TODAY)==0


@pytest.mark.asyncio
async def test_old_report_is_snapshot(db, test_user, history, client, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','false')
    # Legacy SQL date(started_at) depends on database timezone; fix it locally.
    await db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    await service.ensure_reports(db,test_user.id,TODAY)
    await db.commit()
    row = await report(db,test_user.id)
    original = deepcopy(row.payload)
    assert (row.rules_version,row.shape_version)==(2,2)
    assert 'analytics_meta' not in original
    assert original['metrics']['volume']['tonnage_kg']==1119  # Original UTC-start/normal-only selection.
    assert original['metrics']['intensity']['avg_relative']==pytest.approx((120+999)/(80*36/27)/2)
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','true')
    await rebuild_user(db,test_user.id)
    await mark_dirty(db,test_user.id)
    await db.commit()
    assert await service.ensure_reports(db,test_user.id,TODAY)==0
    await db.commit()
    first = (await client.get('/reports/week/2026-08-10')).json()
    assert 'Бржицки' in first['calculation_label']
    assert first['analytics_meta'] is None
    seen = (await client.post('/reports/week/2026-08-10/seen')).json()
    assert (await client.post('/reports/week/2026-08-10/seen')).json()['seen_at']==seen['seen_at']
    await db.refresh(row)
    assert row.payload==original


@pytest.mark.asyncio
@pytest.mark.parametrize('version',[1,2,777])
async def test_labels_use_stored_registered_versions(db,test_user,client,version):
    row = PeriodReport(app_user_id=test_user.id,period_type='week',period_start=START,
        period_end=END,rules_version=version,shape_version=version,payload={'metrics':{},'actions':[]})
    db.add(row)
    await db.commit()
    data=(await client.get('/reports/week/2026-08-10')).json()
    assert data['analytics_meta'] is None
    assert ('Бржицки' in data['calculation_label']) if version in (1,2) else data['calculation_label']=='Создано до обновления аналитики'


@pytest.mark.asyncio
@pytest.mark.parametrize('race',['source','nested'])
async def test_report_discards_incoherent_assembly(db,test_user,history,monkeypatch,race):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','true')
    await rebuild_user(db,test_user.id)
    original = service.calendar_analytics

    async def interleaved(*args):
        resource = await original(*args)
        if race == 'source':
            await mark_dirty(db,test_user.id)
        else:
            for exercise in resource.exercises.values():
                exercise.record_baseline.data_revision += 1
        return resource

    monkeypatch.setattr(service,'calendar_analytics',interleaved)
    assert await service.ensure_reports(db,test_user.id,TODAY)==0
    assert await report(db,test_user.id) is None
    await db.rollback()  # Release injected source mutation before owner-fixture cleanup.


@pytest.mark.asyncio
async def test_background_materializer_obeys_gate(db,test_user,history,monkeypatch):
    from api.services.notification_service import materialize_domain_notifications
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','true')
    await materialize_domain_notifications(db,test_user.id,TODAY)
    await db.commit()
    assert await report(db,test_user.id) is None
    await rebuild_user(db,test_user.id)
    await materialize_domain_notifications(db,test_user.id,TODAY)
    await db.commit()
    assert (await report(db,test_user.id)).rules_version==3
    await materialize_domain_notifications(db,test_user.id,TODAY)
    notices=(await db.execute(select(AppNotification).where(AppNotification.app_user_id==test_user.id,
        AppNotification.event_type=='period_report',AppNotification.dedupe_key=='period_report:week:2026-08-10'))).scalars().all()
    assert len(notices)==1
    await db.commit()


@pytest.mark.asyncio
async def test_existing_snapshot_skips_gate_and_unknown_load_stays_unknown(db,test_user,history,monkeypatch):
    from sqlalchemy import update
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','true')
    # No known body mass: count/RIR survive, tonnage/strength must not become zero.
    instance_ids=select(WorkoutSessionExercise.id).where(WorkoutSessionExercise.exercise_id==history)
    await db.execute(update(WorkoutSessionSet).where(WorkoutSessionSet.workout_session_exercise_id.in_(instance_ids)).values(
        analytics_load_snapshot=dict(schema_version=1,model='bodyweight',input_basis='body_components',added_load_kg=0,assistance_kg=0)))
    await db.commit()
    await rebuild_user(db,test_user.id)
    await service.ensure_reports(db,test_user.id,TODAY)
    await db.commit()
    row=await report(db,test_user.id)
    assert row.payload['metrics']['volume']['tonnage_kg'] is None
    assert row.payload['metrics']['volume']['work_sets']==2
    assert row.payload['metrics']['intensity']['avg_relative'] is None
    assert row.payload['metrics']['records']==[]
    monkeypatch.setattr(service,'closed_periods',lambda *_:[(START,END)])
    async def forbidden(*_):
        pytest.fail('existing snapshot must bypass all current gates')
    monkeypatch.setattr(service,'coherent_read',forbidden)
    assert await service.ensure_reports(db,test_user.id,TODAY)==0


@pytest.mark.asyncio
async def test_service_does_not_commit_snapshots(db,test_user,history,monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','true')
    await rebuild_user(db,test_user.id)
    assert await service.ensure_reports(db,test_user.id,TODAY)>0
    await db.rollback()
    assert await report(db,test_user.id) is None
