"""Remaining outer mutation owners and semantic no-ops."""
import asyncio
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select
from api.services.models import (AnalyticsState, WorkoutSession, WorkoutSessionExercise,
    Exercise, UserAnthropometry, AppUserProfile)
from api.services.workout_superset_service import WorkoutSupersetService as Supersets


async def revision(db, owner):
    return await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == owner)) or 0


async def structure(db, owner):
    workout = WorkoutSession(app_user_id=owner, source='free', status='active')
    db.add(workout)
    exercises = [Exercise(name=f't12-{uuid4()}', category='base', main_muscle_group='chest',
        difficulty='beginner', equipment_needed=[], source='custom', app_user_id=owner) for _ in range(3)]
    db.add_all(exercises)
    await db.flush()
    group = str(uuid4())
    instances = [WorkoutSessionExercise(workout_session_id=workout.id, exercise_id=e.id,
        order_index=i, superset_group=group if i < 2 else None) for i,e in enumerate(exercises)]
    db.add_all(instances)
    await db.commit()
    return workout.id, instances, group


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create', 'add_existing', 'add_new', 'remove', 'delete', 'reorder', 'move_in', 'move_out', 'start', 'cleanup'])
async def test_superset_mutation_dirties_once(db, test_user, operation):
    owner = test_user.id
    workout, rows, group = await structure(db, owner)
    baseline = await revision(db, owner)
    if operation == 'create':
        await Supersets.create_superset(db, workout, owner, rows[0].id, [rows[2].id])
    elif operation == 'add_existing':
        await Supersets.add_existing_exercise_to_superset(db, owner, group, rows[2].id)
    elif operation == 'add_new':
        await Supersets.add_new_exercise_to_superset(db, owner, group, rows[2].exercise_id)
    elif operation == 'remove':
        await Supersets.remove_exercise_from_superset(db, owner, group, rows[0].id)
    elif operation == 'delete':
        await Supersets.delete_superset(db, owner, group)
    elif operation == 'reorder':
        await Supersets.reorder_workout_structure(db, workout, owner,
            [{'type':'exercise', 'session_exercise_id':r.id} for r in reversed(rows)])
    elif operation == 'move_in':
        await Supersets.move_exercise_to_superset(db, workout, owner, rows[2].id, group)
    elif operation == 'move_out':
        await Supersets.move_exercise_out_of_superset(db, workout, owner, rows[0].id)
    elif operation == 'start':
        await Supersets.start_superset(db, rows[2].id)
    else:
        rows[0].superset_group = None
        await db.commit()
        await Supersets.cleanup_single_member_supersets(db, workout, owner)
    assert await revision(db, owner) == baseline + 1


@pytest.mark.asyncio
async def test_superset_noop_and_nested_cleanup_are_one_transaction(db, test_user, monkeypatch):
    workout, rows, group = await structure(db, test_user.id)
    before = await revision(db, test_user.id)
    await Supersets.move_exercise_to_superset(db, workout, test_user.id, rows[0].id, group)
    assert await revision(db, test_user.id) == before
    original = Supersets.cleanup_superset_groups_after_reorder
    async def fail(*args, **kwargs):
        raise RuntimeError('cleanup failed')
    monkeypatch.setattr(Supersets, 'cleanup_superset_groups_after_reorder', fail)
    row_id = rows[0].id
    with pytest.raises(RuntimeError):
        await Supersets.move_exercise_out_of_superset(db, workout, test_user.id, row_id)
    await db.rollback()
    assert await db.scalar(select(WorkoutSessionExercise.superset_group).where(WorkoutSessionExercise.id == row_id)) == group
    monkeypatch.setattr(Supersets, 'cleanup_superset_groups_after_reorder', original)
    await Supersets.move_exercise_out_of_superset(db, workout, test_user.id, row_id)
    assert await revision(db, test_user.id) == before + 1
    await Supersets.move_exercise_out_of_superset(db, workout, test_user.id, row_id)
    assert await revision(db, test_user.id) == before + 1


@pytest.mark.asyncio
async def test_unrelated_concurrent_superset_commit_cannot_dirty_noop(db, test_user, monkeypatch):
    from app.database import SessionLocal
    workout_x, rows_x, _ = await structure(db, test_user.id)
    workout_y, rows_y, _ = await structure(db, test_user.id)
    before = await revision(db, test_user.id)
    original = Supersets.get_workout_session_exercises_map
    interleaved = False

    async def read_then_unrelated_commit(session, workout_id, app_user_id):
        nonlocal interleaved
        result = await original(session, workout_id, app_user_id)
        if session is db and workout_id == workout_x and not interleaved:
            interleaved = True
            # Distinct real connection commits Y after X's initial structure read.
            async with SessionLocal() as competing:
                await Supersets.move_exercise_out_of_superset(
                    competing, workout_y, app_user_id, rows_y[0].id)
            assert await revision(db, app_user_id) == before + 1
        return result

    monkeypatch.setattr(Supersets, 'get_workout_session_exercises_map', read_then_unrelated_commit)
    # X's third exercise is already ungrouped and X has no singleton to clean up.
    await Supersets.move_exercise_out_of_superset(db, workout_x, test_user.id, rows_x[2].id)
    assert interleaved
    assert await revision(db, test_user.id) == before + 1
    assert await db.scalar(select(WorkoutSessionExercise.superset_group).where(
        WorkoutSessionExercise.id == rows_x[2].id)) is None


@pytest.mark.asyncio
async def test_locked_superset_mutation_refreshes_preloaded_target(db, test_user):
    from app.database import SessionLocal
    workout, rows, group = await structure(db, test_user.id)
    before = await revision(db, test_user.id)
    async with SessionLocal() as competing:
        await Supersets.move_exercise_to_superset(competing, workout, test_user.id, rows[2].id, group)
    # This session still has the old ungrouped identity; the locked mutation
    # must operate on the now-committed target, not that cached identity.
    await Supersets.move_exercise_out_of_superset(db, workout, test_user.id, rows[2].id)
    assert await db.scalar(select(WorkoutSessionExercise.superset_group).where(
        WorkoutSessionExercise.id == rows[2].id)) is None
    assert await revision(db, test_user.id) == before + 2


@pytest.mark.asyncio
async def test_center_selection_only_preserves_saved_analytics_context(db, test_user, client):
    from datetime import date
    from tests.integration.test_periodization_calendar import _seed
    from api.services.periodization.repository import ensure_active_block
    from api.services.scheduling_engine import SchedulingEngine
    from api.services.models import UserCalendarDay, UserSplit, SplitDaySlot
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, date.today())
    await SchedulingEngine.generate_block_days(db, test_user.id, block, block.start_date, block.planned_end_date)
    before = await revision(db, test_user.id)
    snapshots = list((await db.execute(select(UserCalendarDay.id, UserCalendarDay.obligation_snapshot).where(
        UserCalendarDay.app_user_id == test_user.id).order_by(UserCalendarDay.id))).all())
    split = await db.scalar(select(UserSplit).where(UserSplit.app_user_id == test_user.id))
    slot = await db.scalar(select(SplitDaySlot.id).where(SplitDaySlot.blueprint_id == split.blueprint_id))
    for endpoint, data in [('/workout-center/context/plan', {'plan_id':None}),
                           ('/workout-center/split-day', {'split_day_id':str(slot)}),
                           ('/workout-center/context/mesocycle', {'mesocycle_id':None}),
                           ('/workout-center/context/microcycle', {'microcycle_id':None})]:
        response = await client.patch(endpoint, json=data)
        assert response.status_code == 200, response.text
        assert await revision(db, test_user.id) == before
    after = list((await db.execute(select(UserCalendarDay.id, UserCalendarDay.obligation_snapshot).where(
        UserCalendarDay.app_user_id == test_user.id).order_by(UserCalendarDay.id))).all())
    assert snapshots == after


@pytest.mark.asyncio
async def test_onboarding_explicit_fields_and_one_dirty(db, client, test_user):
    db.add_all([AppUserProfile(app_user_id=test_user.id),
        UserAnthropometry(app_user_id=test_user.id, weight=80, height=175, submitted_fields=['weight','height'])])
    await db.commit()
    before = await revision(db, test_user.id)
    response = await client.patch('/profile/onboarding', json={'height':180})
    assert response.status_code == 200, response.text
    row = (await db.scalars(select(UserAnthropometry).where(UserAnthropometry.app_user_id == test_user.id).order_by(UserAnthropometry.id.desc()))).first()
    assert row.weight == 80 and row.height == 180 and row.submitted_fields == ['height']
    assert await revision(db, test_user.id) == before + 1
    response = await client.patch('/profile/onboarding', json={'experience_level':'beginner'})
    assert response.status_code == 200
    assert await revision(db, test_user.id) == before + 1


@pytest.mark.asyncio
async def test_standalone_lifecycle_and_noop_dirty_once(db, test_user):
    from tests.integration.test_periodization_calendar import _seed, TODAY
    from api.services.periodization.repository import ensure_active_block
    await _seed(db, test_user.id)
    before = await revision(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    assert await revision(db, test_user.id) == before + 1
    await ensure_active_block(db, test_user.id, TODAY)
    assert await revision(db, test_user.id) == before + 1
    await ensure_active_block(db, test_user.id, block.planned_end_date + timedelta(days=60))
    assert await revision(db, test_user.id) == before + 2


@pytest.mark.asyncio
async def test_refresh_lifecycle_catchup_is_one_outer_revision(db, test_user):
    from tests.integration.test_periodization_calendar import _seed, TODAY
    from api.services.models import AppUserMesocycle, AppUserMicrocycle
    from api.services.periodization.repository import ensure_active_block
    from api.services.periodization.service import refresh_proposals
    await _seed(db, test_user.id)
    meso = await db.scalar(select(AppUserMesocycle).where(AppUserMesocycle.app_user_id == test_user.id))
    meso.microcycle_length = 1
    micro = await db.scalar(select(AppUserMicrocycle).where(AppUserMicrocycle.app_user_id == test_user.id))
    micro.length_days = 1
    await db.commit()
    block = await ensure_active_block(db, test_user.id, TODAY)
    before = await revision(db, test_user.id)
    await refresh_proposals(db, test_user.id, block.planned_end_date + timedelta(days=10))
    assert await revision(db, test_user.id) == before + 1


@pytest.mark.asyncio
async def test_existing_worker_runs_analytics(db, test_user, fresh_exercise, monkeypatch):
    from tests.integration.test_p108_rebuild import history
    from api.services import workout_recalculation as worker
    from api.services.analytics import rebuild
    await history(db, test_user.id, fresh_exercise.id)
    stop = asyncio.Event()
    async def legacy(*args, **kwargs):
        stop.set()
    monkeypatch.setattr(worker, 'run_pending_recalculations', legacy)
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    await worker.workout_recalculation_worker(stop)
    assert await rebuild.active_records(db, test_user.id, fresh_exercise.id, 'all') is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['launch', 'horizon', 'stale'])
async def test_lifecycle_nested_in_calendar_owner_dirties_once(db, test_user, operation):
    from tests.integration.test_periodization_calendar import _seed, TODAY
    from api.services.periodization.repository import ensure_active_block
    from api.services.periodization.service import close_stale_block
    from api.services.scheduling_engine import SchedulingEngine
    from api.services.models import UserSplit
    await _seed(db, test_user.id)
    if operation == 'launch':
        split = await db.scalar(select(UserSplit).where(UserSplit.app_user_id == test_user.id))
        before = await revision(db, test_user.id)
        await SchedulingEngine.launch_and_unroll_plan(db, test_user.id, split.blueprint_id, TODAY, [])
    else:
        block = await ensure_active_block(db, test_user.id, TODAY)
        await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date)
        future = block.planned_end_date + timedelta(days=60)
        before = await revision(db, test_user.id)
        if operation == 'horizon':
            await SchedulingEngine.ensure_horizon(db, test_user.id, future)
        else:
            await close_stale_block(db, test_user.id, future)
    assert await revision(db, test_user.id) == before + 1
