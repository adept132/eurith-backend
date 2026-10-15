from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import select, event
from api.services.models import (WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet,
    AppUserProfile, TrainingBlock, UserCalendarDay, UserAnthropometry, Exercise)
from api.services.analytics.types import AnalyticsQuery, Coverage
from api.services.analytics.service import exercise_analytics, muscle_analytics, block_analytics, compare_blocks

UTC=timezone.utc
CTX=dict(schema_version=1,model='barbell',input_basis='full')

@pytest.fixture
async def p108_seed(app_user, db_session):
    db=db_session
    db.add(AppUserProfile(app_user_id=app_user.id,timezone='Europe/Moscow'))
    exercises=[]
    for name in ('A','Common','B','Unknown'):
        ex=Exercise(name='P108 '+name+' '+str(uuid4()),category='base',main_muscle_group='chest',secondary_muscle_groups=['triceps'],
            difficulty='beginner',equipment_needed=[],source='custom',app_user_id=app_user.id)
        db.add(ex)
        exercises.append(ex)
    blocks=[]
    for i,start in enumerate((date(2026,8,1),date(2026,9,1))):
        block=TrainingBlock(app_user_id=app_user.id,block_index=i,microcycle_length=7,start_date=start,
            planned_end_date=start+timedelta(days=27),actual_end_date=start+timedelta(days=6),status='closed')
        db.add(block)
        blocks.append(block)
    await db.flush()
    gym=uuid4()
    workouts=[]
    for index,block in enumerate(blocks):
        for offset in range(7):
            day=block.start_date+timedelta(days=offset)
            cal=UserCalendarDay(app_user_id=app_user.id,block_id=block.id,target_date=day,is_rest_day=offset>=4,
                obligation_uuid=uuid4(),obligation_origin_date=day,
                obligation_snapshot={'source':'server','block_id':block.id,'reconstructed':False})
            db.add(cal)
            if offset>=4: continue
            start=datetime.combine(day,datetime.min.time(),UTC)+timedelta(hours=10)
            w=WorkoutSession(app_user_id=app_user.id,source='free',status='finished',started_at=start,
                finished_at=start+timedelta(hours=1),training_block_id=block.id,analytics_snapshot={'is_deload':False})
            db.add(w)
            await db.flush()
            cal.actual_workout_session_id=w.id
            workouts.append(w)
            for ex in (exercises[index*2],exercises[1],exercises[3]):
                # Same exercise can occur twice with independent setup identifiers.
                for instance in range(2 if ex==exercises[1] else 1):
                    e=WorkoutSessionExercise(workout_session_id=w.id,exercise_id=ex.id,order_index=instance)
                    db.add(e)
                    await db.flush()
                    weight=100 if offset<2 else (103 if index==0 else 106)
                    db.add(WorkoutSessionSet(workout_session_exercise_id=e.id,set_number=1,weight=Decimal(weight),reps=1,
                        effort_level='medium',gym_profile_id=gym if instance else None,setup_id=uuid4(),
                        analytics_load_snapshot=CTX if ex!=exercises[3] else {'schema_version':1,'model':'bodyweight','input_basis':'body_components','added_load_kg':0,'assistance_kg':0}))
        db.add_all([UserAnthropometry(app_user_id=app_user.id,measured_on=block.start_date,weight=80,submitted_fields=['weight']),
                    UserAnthropometry(app_user_id=app_user.id,measured_on=block.actual_end_date,weight=81,submitted_fields=['weight'])])
    await db.commit()
    return db,app_user.id,exercises,blocks,gym,workouts

@pytest.mark.asyncio
async def test_block_actual_days_and_global_context(p108_seed):
    db,uid,ex,blocks,gym,_=p108_seed
    all_result=await block_analytics(db,uid,blocks[0].id,None)
    local=await block_analytics(db,uid,blocks[0].id,str(gym))
    assert all_result.volume.calendar_days == 7
    assert all_result.volume.work_sets == 16
    assert local.volume.work_sets == 4
    assert local.body == all_result.body and local.adherence == all_result.adherence
    assert local.body.scope == local.adherence.scope == 'block_context'
    assert local.body.metrics['weight'].delta == 1
    assert local.adherence.percent == 100
    assert all_result.exercises[ex[3].id].summary.strength.delta_pct is None
    assert all_result.exercises[ex[3].id].summary.effort.mean_rir.value == 2
    assert all_result.exercises[ex[3].id].summary.volume.tonnage.value is None

@pytest.mark.asyncio
async def test_same_exercise_not_machine_variants(p108_seed):
    db,uid,ex,blocks,gym,_=p108_seed
    result=await exercise_analytics(db,uid,ex[1].id,AnalyticsQuery(period='block',block_id=blocks[0].id))
    assert len(result.points)==4
    assert result.summary.strength.delta_pct == pytest.approx(3)
    assert result.summary.volume.work_sets==8
    assert all(len(p.details.observations)==2 for p in result.points)
    muscles=await muscle_analytics(db,uid,AnalyticsQuery(period='block',block_id=blocks[0].id))
    chest=next(m for m in muscles.muscles if m.muscle_key=='chest')
    assert len(chest.exercise_ids)==2
    assert chest.coverage.used==2 and chest.coverage.total==3

@pytest.mark.asyncio
async def test_common_intersection_and_percentage_points(p108_seed):
    db,uid,ex,blocks,_,_=p108_seed
    result=await compare_blocks(db,uid,blocks[0].id,blocks[1].id,None)
    assert result.common_exercise_ids==[ex[1].id]
    assert result.added==[ex[2].id] and result.removed==[ex[0].id]
    assert result.differences.muscles['chest'].value==pytest.approx(3)
    assert result.differences.muscles['chest'].unit=='percentage_points'
    assert result.a.muscles[0].exercise_ids==(ex[1].id,)
    assert result.b.muscles[0].exercise_ids==(ex[1].id,)
    assert result.model_dump(mode='json')['rules_version']=='p1-08-v1'

@pytest.mark.asyncio
async def test_unknown_end_preserves_other_side_and_proven_facts(p108_seed):
    db,uid,ex,blocks,_,_=p108_seed
    blocks[0].actual_end_date=None
    await db.commit()
    result=await compare_blocks(db,uid,blocks[0].id,blocks[1].id,None)
    assert result.a.scope.period is None and result.a.scope.reason=='unknown_block_end'
    assert result.a.block.start_date==date(2026,8,1)
    assert result.a.volume.work_sets==16 and result.a.volume.calendar_days is None
    assert result.a.volume.work_sets_per_7_days is None
    assert result.a.body.metrics['weight'].before.value==80
    assert result.a.body.metrics['weight'].after is None
    assert result.a.adherence.percent is None
    assert result.b.volume.calendar_days==7
    assert result.common_exercise_ids==[]

@pytest.mark.asyncio
async def test_query_budgets(p108_seed):
    db,uid,ex,blocks,_,_=p108_seed
    preview=WorkoutSession(app_user_id=uid,source='free',status='active',started_at=datetime(2026,8,2,12,tzinfo=UTC))
    db.add(preview)
    await db.commit()
    statements=[]
    def track(conn,cursor,statement,params,context,many):
        if statement.lstrip().upper().startswith('SELECT'): statements.append(statement)
    event.listen(db.bind.sync_engine,'before_cursor_execute',track)
    try:
        await compare_blocks(db,uid,blocks[0].id,blocks[1].id,None)
        assert len(statements)<=18
        statements.clear()
        await exercise_analytics(db,uid,ex[1].id,AnalyticsQuery(period='block',block_id=blocks[0].id))
        assert len(statements)==9
        statements.clear()
        await exercise_analytics(db,uid,ex[1].id,AnalyticsQuery(period='block',block_id=blocks[0].id,preview_workout_id=preview.id))
        assert len(statements)==10
    finally:
        event.remove(db.bind.sync_engine,'before_cursor_execute',track)


@pytest.mark.asyncio
async def test_preview_baseline_separate_from_full_graph_chronology(p108_seed):
    db,uid,ex,blocks,_,workouts=p108_seed
    preview=WorkoutSession(app_user_id=uid,source='free',status='active',started_at=datetime(2026,8,2,12,tzinfo=UTC))
    db.add(preview)
    await db.commit()
    # An intermediate 103 result on Aug 3 must suppress a false PR on Aug 4,
    # even though it is after the preview cutoff and before the visible range.
    result=await exercise_analytics(db,uid,ex[1].id,AnalyticsQuery(period='custom',
        start=date(2026,8,4),end=date(2026,8,4),preview_workout_id=preview.id))
    assert result.record_baseline.best_e1rm==100
    assert result.record_baseline.as_of==preview.started_at
    assert result.record_baseline.excluded_workout_id==preview.id
    assert result.record_events==[]
    assert len(result.points)==1

@pytest.mark.asyncio
async def test_muscle_details_retain_independent_metric_sources(p108_seed):
    db,uid,ex,blocks,_,_=p108_seed
    result=await muscle_analytics(db,uid,AnalyticsQuery(period='block',block_id=blocks[0].id))
    part=next(p for m in result.muscles for p in m.contributions if p.exercise_id==ex[3].id)
    point=part.observations[0]
    assert point.details.metric_coverage['strength'].used==0
    assert point.details.metric_coverage['effort'].used==1
    assert point.details.observations[0].rir==2
    assert point.details.observations[0].set_id is not None

@pytest.mark.asyncio
async def test_short_history_deload_preserves_records_and_volume(p108_seed):
    db,uid,ex,blocks,_,workouts=p108_seed
    workouts[3].analytics_snapshot={'is_deload':True}
    await db.commit()
    result=await exercise_analytics(db,uid,ex[1].id,AnalyticsQuery(period='block',block_id=blocks[0].id))
    assert result.summary.strength_reason=='insufficient_sessions'
    assert result.summary.strength.delta_pct is None
    assert result.summary.best_e1rm.value==103
    assert result.summary.volume.work_sets==8
    assert len(result.points)==4 and result.points[-1].is_deload
    assert result.record_events
    assert any(e.reason=='deload_excluded_from_strength_change' for e in result.excluded)

@pytest.mark.asyncio
async def test_unknown_end_excludes_unlinked_date_coincidence(p108_seed):
    from api.services.analytics.repository import load_facts
    db,uid,ex,blocks,_,workouts=p108_seed
    blocks[0].actual_end_date=None
    workouts[0].training_block_id=None
    await db.commit()
    with pytest.raises(ValueError,match='unknown_block_end'):
        await load_facts(db,uid,AnalyticsQuery(period='block',block_id=blocks[0].id))
    result=await block_analytics(db,uid,blocks[0].id,None)
    assert result.volume.work_sets==12
    assert len(result.exercises[ex[1].id].points)==3
    assert result.scope.timezone=='Europe/Moscow'
    assert result.volume.tonnage_per_7_days.reason=='unknown_block_end'

@pytest.mark.asyncio
async def test_point_sources_use_profile_dates_and_independent_coverage(p108_seed):
    db,uid,ex,blocks,_,workouts=p108_seed
    w=workouts[1]
    w.started_at=datetime(2026,8,1,21,tzinfo=UTC)
    w.finished_at=datetime(2026,8,1,22,tzinfo=UTC)
    rows=list((await db.scalars(select(WorkoutSessionSet).join(WorkoutSessionExercise).where(
        WorkoutSessionExercise.workout_session_id==w.id,WorkoutSessionExercise.exercise_id==ex[1].id))).all())
    rows[0].weight=200
    rows[0].analytics_load_snapshot={'schema_version':1,'model':'dumbbell','input_basis':'per_implement','rep_basis':'simultaneous'}
    rows[0].effort_level=None
    rows[1].effort_level='failure'
    await db.commit()
    result=await exercise_analytics(db,uid,ex[1].id,AnalyticsQuery(period='block',block_id=blocks[0].id))
    point=next(p for p in result.points if p.workout_id==w.id)
    assert point.e1rm==200 and point.source_set_id==rows[0].id
    assert point.details.metric_coverage['volume']==Coverage(1,2)
    assert point.details.effort.value==0
    assert point.details.metric_coverage['effort'].used==1
    assert point.details.observation_dates==(date(2026,8,2),)
    assert {o.observation_date for o in point.details.observations}=={date(2026,8,2)}

@pytest.mark.asyncio
async def test_adherence_difference_uses_unrounded_counts(p108_seed):
    db,uid,_,blocks,_,_=p108_seed
    days=list((await db.scalars(select(UserCalendarDay).where(UserCalendarDay.app_user_id==uid))).all())
    for day in days:
        block=next(b for b in blocks if b.id==day.block_id)
        offset=(day.target_date-block.start_date).days
        if offset==3: day.is_rest_day=True
        if offset in ((1,2) if block==blocks[0] else (2,)):
            day.actual_workout_session_id=None
    await db.commit()
    result=await compare_blocks(db,uid,blocks[0].id,blocks[1].id,None)
    assert (result.a.adherence.percent,result.b.adherence.percent)==(33,67)
    assert result.differences.adherence.value==pytest.approx(100/3)

@pytest.mark.asyncio
async def test_empty_and_owner_visibility(p108_seed):
    db,uid,ex,blocks,_,_=p108_seed
    empty=await exercise_analytics(db,uid,ex[1].id,AnalyticsQuery(period='custom',start=date(2020,1,1),end=date(2020,1,2)))
    assert empty.points==[] and empty.summary.best_e1rm.value is None
    assert empty.summary.volume.tonnage.value==0
    assert empty.record_baseline.best_e1rm==106
    with pytest.raises(ValueError,match='exercise_not_found'):
        await exercise_analytics(db,uid+999999,ex[1].id,AnalyticsQuery())
    with pytest.raises(ValueError,match='block_not_found'):
        await block_analytics(db,uid+999999,blocks[0].id,None)


@pytest.mark.asyncio
async def test_visible_exercise_without_any_history(p108_seed):
    db,uid,_,_,_,_=p108_seed
    ex=Exercise(name='Empty '+str(uuid4()),category='base',main_muscle_group='chest',difficulty='beginner',
        equipment_needed=[],source='custom',app_user_id=uid)
    db.add(ex)
    await db.commit()
    result=await exercise_analytics(db,uid,ex.id,AnalyticsQuery(gym_id='unknown'))
    assert result.points==[] and result.record_baseline.best_e1rm is None
    assert result.scope.gym_scope=='unknown'
    with pytest.raises(ValueError,match='exercise_not_found'):
        await exercise_analytics(db,uid+999999,ex.id,AnalyticsQuery())

@pytest.mark.asyncio
async def test_empty_intersection_keeps_global_context(p108_seed):
    db,uid,_,blocks,_,_=p108_seed
    result=await compare_blocks(db,uid,blocks[0].id,blocks[1].id,str(uuid4()))
    assert result.common_exercise_ids==[]
    assert any(e.reason=='no_common_exercises' for e in result.excluded)
    assert result.a.volume.work_sets==result.b.volume.work_sets==0
    assert result.a.body.metrics['weight'].delta==result.b.body.metrics['weight'].delta==1
    assert result.a.adherence.percent==result.b.adherence.percent==100
