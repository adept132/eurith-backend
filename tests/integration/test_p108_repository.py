from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import event
from api.services.models import (WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet,
    AppUserProfile, TrainingBlock, UserCalendarDay, UserAnthropometry, Exercise)
from api.services.analytics.repository import load_facts
from api.services.analytics.types import AnalyticsQuery

UTC = timezone.utc
DAY = date(2026, 10, 15)
QUERY = AnalyticsQuery(period='custom', start=DAY, end=DAY)
CTX = dict(schema_version=1, model='barbell', input_basis='full')

async def add_session(db, owner, exercise, start, finish=None, count=1, **kwargs):
    w = WorkoutSession(app_user_id=owner, source='free', status='finished', started_at=start,
                       finished_at=finish or start + timedelta(hours=1), **kwargs)
    db.add(w)
    await db.flush()
    e = WorkoutSessionExercise(workout_session_id=w.id, exercise_id=exercise, order_index=0)
    db.add(e)
    await db.flush()
    db.add_all([WorkoutSessionSet(workout_session_exercise_id=e.id, set_number=i+1,
        weight=Decimal('50'), reps=10, analytics_load_snapshot=CTX) for i in range(count)])
    await db.flush()
    return w

@pytest.mark.asyncio
async def test_local_finished_date_and_two_sessions(db_session, app_user, exercise):
    db = db_session
    db.add(AppUserProfile(app_user_id=app_user.id, timezone='Europe/Moscow'))
    a = await add_session(db, app_user.id, exercise.id, datetime(2026,10,14,20,tzinfo=UTC), datetime(2026,10,14,22,tzinfo=UTC))
    b = await add_session(db, app_user.id, exercise.id, datetime(2026,10,15,12,tzinfo=UTC))
    await add_session(db, app_user.id, exercise.id, datetime(2026,10,15,21,tzinfo=UTC))
    facts = await load_facts(db, app_user.id, QUERY)
    assert [s.workout_id for s in facts.window_sets] == [a.id, b.id]
    assert all(isinstance(s.weight_kg, Decimal) and s.finished_at.tzinfo for s in facts.window_sets)

@pytest.mark.asyncio
async def test_reference_and_records_before_visible_period(db_session, app_user, exercise):
    db = db_session
    target = datetime(2026,10,15,12,tzinfo=UTC)
    old = await add_session(db, app_user.id, exercise.id, target-timedelta(days=100))
    edge = await add_session(db, app_user.id, exercise.id, target-timedelta(days=56))
    for i in range(1, 15):
        await add_session(db, app_user.id, exercise.id, target-timedelta(days=i))
    visible = await add_session(db, app_user.id, exercise.id, target)
    facts = await load_facts(db, app_user.id, QUERY)
    assert len(facts.window_sets) == 1
    assert edge.id in {s.workout_id for s in facts.reference_sets}
    assert old.id not in {s.workout_id for s in facts.reference_sets}
    assert old.id in {s.workout_id for s in facts.record_sets}
    assert visible.id not in {s.workout_id for s in facts.reference_sets}

@pytest.mark.asyncio
async def test_unknown_deload_is_not_inferred(db_session, app_user, exercise):
    db = db_session
    block = TrainingBlock(app_user_id=app_user.id, block_index=0, microcycle_length=7,
        start_date=DAY, planned_end_date=DAY, phases=[dict(phase_number=1, effort_tier='deload')])
    db.add(block)
    await db.flush()
    a = await add_session(db, app_user.id, exercise.id, datetime(2026,10,15,10,tzinfo=UTC))
    b = await add_session(db, app_user.id, exercise.id, datetime(2026,10,15,12,tzinfo=UTC),
        training_block_id=block.id, mesocycle_phase=1, analytics_snapshot=dict(schema_version=1,is_deload=None,body_mass=None))
    c = await add_session(db, app_user.id, exercise.id, datetime(2026,10,15,14,tzinfo=UTC), training_block_id=block.id,mesocycle_phase=1)
    facts = await load_facts(db, app_user.id, QUERY)
    assert {s.workout_id:s.is_deload for s in facts.window_sets} == {a.id:None,b.id:None,c.id:True}
    assert a.analytics_snapshot is None and b.analytics_snapshot['body_mass'] is None

@pytest.mark.asyncio
@pytest.mark.parametrize('with_preview', [False, True])
async def test_query_count_independent_of_sets(db_session, app_user, exercise, with_preview):
    db = db_session
    w = await add_session(db, app_user.id, exercise.id, datetime(2026,10,15,12,tzinfo=UTC),count=10)
    from dataclasses import replace
    query = replace(QUERY, preview_workout_id=w.id) if with_preview else QUERY
    counts = []
    statements = []
    def track(conn, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)
    for size in (10,1000):
        if size == 1000:
            from sqlalchemy import select
            eid = await db.scalar(select(WorkoutSessionExercise.id).where(WorkoutSessionExercise.workout_session_id==w.id))
            db.add_all([WorkoutSessionSet(workout_session_exercise_id=eid,set_number=i+1,weight=50,reps=10,analytics_load_snapshot=CTX) for i in range(10,1000)])
            await db.flush()
        statements.clear()
        event.listen(db.bind.sync_engine, 'before_cursor_execute', track)
        try:
            facts = await load_facts(db, app_user.id, query, exercise_ids={exercise.id})
        finally:
            event.remove(db.bind.sync_engine, 'before_cursor_execute', track)
        assert len(facts.window_sets) == size
        counts.append(len(statements))
    assert counts[0] == counts[1] and counts[1] <= 12
    print('repository SELECT counts 10/1000:', counts)

@pytest.mark.asyncio
async def test_preview_is_owned_strict_and_does_not_cut_graph(db_session, app_user, exercise):
    db = db_session
    start = datetime(2026,10,15,12,tzinfo=UTC)
    prior = await add_session(db, app_user.id, exercise.id,start-timedelta(hours=3))
    preview = await add_session(db, app_user.id, exercise.id,start)
    same = await add_session(db, app_user.id, exercise.id,start)
    later = await add_session(db, app_user.id, exercise.id,start+timedelta(hours=3))
    q = AnalyticsQuery(period='custom',start=DAY,end=DAY,preview_workout_id=preview.id)
    facts = await load_facts(db,app_user.id,q)
    assert {s.workout_id for s in facts.record_sets} == {prior.id}
    assert {s.workout_id for s in facts.window_sets} == {prior.id,preview.id,same.id,later.id}
    with pytest.raises(ValueError,match='preview_workout_not_found'):
        await load_facts(db,app_user.id+999999,q)

@pytest.mark.asyncio
async def test_global_sidecars_and_unknown_gym(db_session, app_user, exercise):
    from sqlalchemy import select
    db = db_session
    a = await add_session(db,app_user.id,exercise.id,datetime(2026,10,15,10,tzinfo=UTC),gym_snapshot={'name':'Saved gym'})
    b = await add_session(db,app_user.id,exercise.id,datetime(2026,10,15,12,tzinfo=UTC))
    gym, setup = uuid4(),uuid4()
    s = (await db.execute(select(WorkoutSessionSet).join(WorkoutSessionExercise).where(
        WorkoutSessionExercise.workout_session_id==b.id))).scalar_one()
    s.gym_profile_id, s.setup_id = gym,setup
    s.load_snapshot = {'gym_name':'Historical label'}
    empty = WorkoutSession(app_user_id=app_user.id,source='free',status='finished',
        started_at=datetime(2026,10,15,16,tzinfo=UTC),finished_at=datetime(2026,10,15,17,tzinfo=UTC))
    db.add(empty)
    other = Exercise(name='Independent catalog',category='base',main_muscle_group='biceps',
        difficulty='beginner',equipment_needed=[],source='custom',app_user_id=app_user.id)
    db.add(other)
    await db.flush()
    db.add(UserCalendarDay(app_user_id=app_user.id,target_date=DAY,actual_workout_session_id=empty.id))
    await db.flush()
    def q(gym_id):
        return AnalyticsQuery(period='custom',start=DAY,end=DAY,gym_id=gym_id)
    all_facts = await load_facts(db,app_user.id,q(None),exercise_ids={exercise.id})
    unknown = await load_facts(db,app_user.id,q('unknown'),exercise_ids={exercise.id})
    known = await load_facts(db,app_user.id,q(str(gym)),exercise_ids={exercise.id})
    assert {s.workout_id for s in all_facts.window_sets} == {a.id,b.id}
    assert [s.workout_id for s in unknown.window_sets] == [a.id]
    assert [s.workout_id for s in known.window_sets] == [b.id]
    assert known.window_sets[0].gym_label == 'Historical label'
    assert unknown.window_sets[0].gym_label == 'Saved gym'
    assert known.window_sets[0].setup_id == str(setup)
    assert known.window_sets[0].comparison_key == unknown.window_sets[0].comparison_key
    assert {w['id'] for w in known.global_workouts} == {a.id,b.id,empty.id}
    assert other.id in {c['exercise_id'] for c in known.catalog_mappings}
    assert known.calendar[0]['actual_workout_session_id'] == empty.id
    assert await load_facts(db,app_user.id,q(str(uuid4())),exercise_ids=set())

@pytest.mark.asyncio
async def test_block_ownership_actual_end_and_all_unfiltered(db_session,app_user,exercise):
    db = db_session
    b = TrainingBlock(app_user_id=app_user.id,block_index=0,microcycle_length=7,
        start_date=date(2026,1,1),planned_end_date=date(2026,2,1),status='closed')
    db.add(b)
    await db.flush()
    q = AnalyticsQuery(period='block',block_id=b.id)
    with pytest.raises(ValueError,match='unknown_block_end'):
        await load_facts(db,app_user.id,q)
    with pytest.raises(ValueError,match='block_not_found'):
        await load_facts(db,app_user.id+999999,q)
    b.actual_end_date = date(2026,1,10)
    await db.flush()
    assert (await load_facts(db,app_user.id,q)).period.end == date(2026,1,10)
    await add_session(db,app_user.id,exercise.id,datetime(2020,1,1,12,tzinfo=UTC))
    await add_session(db,app_user.id,exercise.id,datetime(2090,1,1,12,tzinfo=UTC))
    f = await load_facts(db,app_user.id,AnalyticsQuery(period='all',gym_id=str(uuid4())),exercise_ids=set())
    assert f.period.start == date(2020,1,1) and not f.window_sets

@pytest.mark.asyncio
async def test_measurements_boundaries_local_fallback_and_explicit_fields(db_session,app_user,exercise):
    from api.services.models import BodyMeasurement
    db = db_session
    db.add(AppUserProfile(app_user_id=app_user.id,timezone='Europe/Moscow'))
    db.add_all([
        UserAnthropometry(app_user_id=app_user.id,measured_on=DAY-timedelta(days=7),weight=80,height=180,submitted_fields=['weight']),
        UserAnthropometry(app_user_id=app_user.id,measured_on=DAY+timedelta(days=7),body_fat=20,submitted_fields=['body_fat']),
        UserAnthropometry(app_user_id=app_user.id,measured_on=DAY,weight=999,submitted_fields=None),
        BodyMeasurement(app_user_id=app_user.id,metric_key='waist',value=81,recorded_at=datetime(2026,10,7,22,tzinfo=UTC)),
        BodyMeasurement(app_user_id=app_user.id,metric_key='waist',value=99,measured_on=DAY-timedelta(days=8))])
    await db.flush()
    facts = await load_facts(db,app_user.id,QUERY)
    assert {(r['metric_key'],r['value']) for r in facts.measurements} == {('weight',Decimal(80)),('body_fat',Decimal(20)),('waist',Decimal(81))}
    fallback = next(r for r in facts.measurements if r['metric_key']=='waist')
    assert fallback['observation_date'] == date(2026,10,8) and fallback['estimated']

@pytest.mark.asyncio
async def test_read_preserves_unknowns_and_normalizes_side_reps(db_session,app_user,exercise):
    from sqlalchemy import select
    db = db_session
    w = await add_session(db,app_user.id,exercise.id,datetime(2026,10,15,12,tzinfo=UTC),
        analytics_snapshot={'schema_version':1,'body_mass':None,'is_deload':None})
    s = (await db.execute(select(WorkoutSessionSet).join(WorkoutSessionExercise).where(
        WorkoutSessionExercise.workout_session_id==w.id))).scalar_one()
    s.reps = 20
    s.analytics_load_snapshot = dict(schema_version=1,model='dumbbell',input_basis='per_implement',
        rep_basis='total',left_reps=10,right_reps=10)
    db.add(UserAnthropometry(app_user_id=app_user.id,weight=90,measured_on=DAY-timedelta(days=1),submitted_fields=['weight']))
    await db.flush()
    writes=[]
    def track(conn,cursor,statement,params,context,many):
        if statement.lstrip().upper().startswith(('UPDATE','INSERT','DELETE')):
            writes.append(statement)
    event.listen(db.bind.sync_engine,'before_cursor_execute',track)
    try:
        facts=await load_facts(db,app_user.id,QUERY)
    finally:
        event.remove(db.bind.sync_engine,'before_cursor_execute',track)
    assert not writes
    f=facts.window_sets[0]
    assert f.e1rm_reps == 10 and f.reps == 20 and f.tonnage_kg == 1000
    assert f.session_context['body_mass'] is None

@pytest.mark.asyncio
async def test_saved_calendar_deload_requires_unique_fact_link(db_session,app_user,exercise):
    db=db_session
    a=await add_session(db,app_user.id,exercise.id,datetime(2026,10,15,10,tzinfo=UTC))
    b=await add_session(db,app_user.id,exercise.id,datetime(2026,10,15,12,tzinfo=UTC))
    db.add_all([UserCalendarDay(app_user_id=app_user.id,target_date=DAY,meso_tag='deload',actual_workout_session_id=a.id),
        UserCalendarDay(app_user_id=app_user.id,target_date=DAY,meso_tag='deload',actual_workout_session_id=b.id),
        UserCalendarDay(app_user_id=app_user.id,target_date=DAY,meso_tag='hard',actual_workout_session_id=b.id)])
    await db.flush()
    facts=await load_facts(db,app_user.id,QUERY)
    assert {f.workout_id:f.is_deload for f in facts.window_sets} == {a.id:True,b.id:None}


@pytest.mark.asyncio
async def test_reference_uses_finish_and_strict_upper_boundary(db_session,app_user,exercise):
    db=db_session
    target=datetime(2026,10,15,12,tzinfo=UTC)
    edge=target-timedelta(days=56)
    included=await add_session(db,app_user.id,exercise.id,edge-timedelta(hours=2),edge)
    equal=await add_session(db,app_user.id,exercise.id,target-timedelta(hours=2),target)
    unfinished=await add_session(db,app_user.id,exercise.id,target-timedelta(hours=1),target+timedelta(hours=1))
    preview=WorkoutSession(app_user_id=app_user.id,source='free',status='active',started_at=target)
    db.add(preview)
    await db.flush()
    q=AnalyticsQuery(period='custom',start=DAY+timedelta(days=1),end=DAY+timedelta(days=1),preview_workout_id=preview.id)
    f=await load_facts(db,app_user.id,q)
    assert {s.workout_id for s in f.reference_sets} == {included.id}
    assert {s.workout_id for s in f.record_sets} == {included.id}

@pytest.mark.asyncio
async def test_completion_between_batches_does_not_lose_joined_context(db_session,app_user,exercise,monkeypatch):
    from api.services.analytics import repository
    original=repository._rows
    inserted=[]
    async def interleave(db,statement):
        rows=await original(db,statement)
        if not inserted and str(statement).startswith('SELECT workout_sessions.'):
            inserted.append(await add_session(db,app_user.id,exercise.id,datetime(2026,10,15,12,tzinfo=UTC)))
        return rows
    monkeypatch.setattr(repository,'_rows',interleave)
    facts=await load_facts(db_session,app_user.id,QUERY)
    assert [f.workout_id for f in facts.window_sets] == [inserted[0].id]
