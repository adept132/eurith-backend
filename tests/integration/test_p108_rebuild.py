"""Real PostgreSQL publication, restart and historical immutability contracts."""
from datetime import datetime, timedelta, timezone, date
from importlib import import_module, util
from uuid import uuid4

import pytest
from sqlalchemy import select, func, update, delete

from app.database import SessionLocal
from api.services.models import (AnalyticsState, AnalyticsRecordProjection, WorkoutSession,
    WorkoutSessionExercise, WorkoutSessionSet, UserExerciseProgressionState, PeriodReport,
    UserAnthropometry)
from api.services.analytics.state import mark_dirty, read_state
from api.services.analytics.repository import load_facts
from api.services.analytics.types import AnalyticsQuery
from api.services.analytics.intensity import intensity_summary

CTX = dict(schema_version=1, model='barbell', input_basis='full')


def implementation():
    assert util.find_spec('api.services.analytics.rebuild'), 'analytics rebuild is missing'
    return import_module('api.services.analytics.rebuild')


async def history(db, owner, exercise, weights=(100, 110, 120)):
    result = []
    for index, weight in enumerate(weights):
        start = datetime.now(timezone.utc) - timedelta(days=10-index)
        workout = WorkoutSession(app_user_id=owner, source='free', status='finished',
            started_at=start, finished_at=start+timedelta(hours=1))
        db.add(workout)
        await db.flush()
        instance = WorkoutSessionExercise(workout_session_id=workout.id, exercise_id=exercise, order_index=0)
        db.add(instance)
        await db.flush()
        row = WorkoutSessionSet(workout_session_exercise_id=instance.id, set_number=1,
            weight=weight, reps=5, is_completed=True, analytics_load_snapshot=CTX)
        db.add(row)
        await db.flush()
        result.append((workout.id, row.id))
    await mark_dirty(db, owner)
    await db.commit()
    return result


@pytest.mark.asyncio
async def test_mutation_during_rebuild_blocks_publication(db, test_user, fresh_exercise, monkeypatch):
    rebuild = implementation()
    rows = await history(db, test_user.id, fresh_exercise.id)
    first = await rebuild.rebuild_user(db, test_user.id)
    await mark_dirty(db, test_user.id)
    await db.commit()
    original = rebuild._publish
    async def mutate_then_publish(*args, **kwargs):
        async with SessionLocal() as other:
            await other.execute(update(WorkoutSessionSet).where(WorkoutSessionSet.id == rows[-1][1]).values(weight=130))
            await mark_dirty(other, test_user.id)
            await other.commit()
        return await original(*args, **kwargs)
    monkeypatch.setattr(rebuild, '_publish', mutate_then_publish)
    result = await rebuild.rebuild_user(db, test_user.id)
    assert result['status'] == 'superseded'
    state = await read_state(db, test_user.id)
    assert str(state.active_generation) == first['generation']
    assert state.active_revision < state.source_revision and state.status == 'pending'
    assert await rebuild.active_records(db, test_user.id, fresh_exercise.id, 'all') is None
    monkeypatch.setattr(rebuild, '_publish', original)
    await rebuild.rebuild_user(db, test_user.id)
    state = await read_state(db, test_user.id)
    assert state.active_revision == state.source_revision and state.status == 'ready'


@pytest.mark.asyncio
async def test_readiness_is_read_only_feature_and_revision_gate(db, test_user, fresh_exercise, monkeypatch):
    from api.services.analytics import state as states
    assert hasattr(states, 'readiness'), 'route-independent readiness is missing'
    monkeypatch.delenv('P108_ANALYTICS_ENABLED', raising=False)
    assert (await states.readiness(db, test_user.id))['state'] == 'disabled'
    assert await db.scalar(select(func.count()).select_from(AnalyticsState).where(AnalyticsState.app_user_id == test_user.id)) == 0
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    assert (await states.readiness(db, test_user.id))['state'] == 'rebuilding'
    await history(db, test_user.id, fresh_exercise.id)
    await implementation().rebuild_user(db, test_user.id)
    ready = await states.readiness(db, test_user.id)
    assert ready['state'] == 'ready' and ready['data_revision'] == 1
    assert (await states.readiness(db, test_user.id, expected_revision=0))['state'] == 'rebuilding'
    await mark_dirty(db, test_user.id)
    await db.commit()
    assert (await states.readiness(db, test_user.id))['state'] == 'rebuilding'


@pytest.mark.asyncio
async def test_worker_recovers_crashed_generation_and_disabled_is_noop(db, test_user, fresh_exercise, monkeypatch):
    rebuild = implementation()
    await history(db, test_user.id, fresh_exercise.id)
    monkeypatch.delenv('P108_ANALYTICS_ENABLED', raising=False)
    assert await rebuild.run_pending_analytics_rebuilds() == 0
    original = rebuild._publish
    async def crash(*args, **kwargs):
        raise RuntimeError('process failure')
    monkeypatch.setattr(rebuild, '_publish', crash)
    with pytest.raises(RuntimeError):
        await rebuild.rebuild_user(db, test_user.id)
    await db.rollback()
    monkeypatch.setattr(rebuild, '_publish', original)
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    assert await rebuild.run_pending_analytics_rebuilds(limit=1000) >= 1
    assert (await read_state(db, test_user.id)).status == 'ready'


@pytest.mark.asyncio
async def test_cli_dry_run_apply_resume(db, test_user, fresh_exercise):
    import asyncio
    import json
    import sys
    from pathlib import Path
    assert Path('scripts/rebuild_analytics.py').exists(), 'CLI is missing'
    await history(db, test_user.id, fresh_exercise.id)
    async def cli(*args):
        process = await asyncio.create_subprocess_exec(sys.executable, 'scripts/rebuild_analytics.py',
            '--user-id', str(test_user.id), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        assert process.returncode == 0, err.decode()
        return json.loads(out.decode())
    before = (await read_state(db, test_user.id)).source_revision
    await db.rollback()
    dry = await cli('--dry-run')
    assert dry['status'] == 'dry_run' and dry['sets'] == 3
    state = await read_state(db, test_user.id)
    assert state.source_revision == before and state.active_generation is None
    assert await db.scalar(select(func.count()).select_from(AnalyticsRecordProjection).where(AnalyticsRecordProjection.app_user_id == test_user.id)) == 0
    await db.rollback()
    applied = await cli('--apply')
    resumed = await cli('--apply', '--resume')
    assert applied['generation'] == resumed['generation'] and applied['events'] == resumed['events']


@pytest.mark.asyncio
async def test_crash_then_resume_is_idempotent(db, test_user, fresh_exercise, monkeypatch):
    rebuild = implementation()
    await history(db, test_user.id, fresh_exercise.id)
    first = await rebuild.rebuild_user(db, test_user.id)
    await mark_dirty(db, test_user.id)
    await db.commit()
    original = rebuild._publish
    async def crash(*args, **kwargs):
        raise RuntimeError('simulated process failure')
    monkeypatch.setattr(rebuild, '_publish', crash)
    with pytest.raises(RuntimeError):
        await rebuild.rebuild_user(db, test_user.id)
    state = await read_state(db, test_user.id)
    generation = state.building_generation
    assert state.status == 'rebuilding' and state.cursor['projections'] == 2
    assert str(state.active_generation) == first['generation']
    monkeypatch.setattr(rebuild, '_publish', original)
    resumed = await rebuild.rebuild_user(db, test_user.id)
    assert resumed['generation'] == str(generation)
    payload = await rebuild.active_records(db, test_user.id, fresh_exercise.id, 'all')
    assert len(payload['events']) == 2 and payload['initial']['weight'] == 100
    again = await rebuild.rebuild_user(db, test_user.id)
    assert again['generation'] == resumed['generation']
    assert await db.scalar(select(func.count()).select_from(AnalyticsRecordProjection).where(
        AnalyticsRecordProjection.generation == generation)) == 2


@pytest.mark.asyncio
async def test_history_edit_updates_future_reference(db, test_user, fresh_exercise):
    rebuild = implementation()
    rows = await history(db, test_user.id, fresh_exercise.id, weights=(150, 100, 120))
    await rebuild.rebuild_user(db, test_user.id)
    before = await load_facts(db, test_user.id, AnalyticsQuery(period='all'))
    target = [r for r in before.graph_sets if r.workout_id == rows[-1][0]]
    old_intensity = intensity_summary(target, before.reference_sets).value
    await db.execute(delete(WorkoutSessionSet).where(WorkoutSessionSet.id == rows[0][1]))
    await db.execute(update(WorkoutSessionSet).where(WorkoutSessionSet.id == rows[1][1]).values(weight=90))
    await mark_dirty(db, test_user.id)
    await db.commit()
    await rebuild.rebuild_user(db, test_user.id)
    payload = await rebuild.active_records(db, test_user.id, fresh_exercise.id, 'all')
    assert payload['initial']['weight'] == 90 and len(payload['events']) == 1
    after = await load_facts(db, test_user.id, AnalyticsQuery(period='all'))
    assert intensity_summary(target, after.reference_sets).value > old_intensity


@pytest.mark.asyncio
async def test_legacy_projection_and_reports_unchanged(db, test_user, fresh_exercise, monkeypatch):
    rebuild = implementation()
    from api.services import notification_service
    from api.services.models import UserRecord, AppNotification
    async def forbidden(*args, **kwargs):
        pytest.fail('historical rebuild must not notify')
    for name in dir(notification_service):
        if name.startswith(('create_', 'send_', 'notify_')):
            monkeypatch.setattr(notification_service, name, forbidden)
    await history(db, test_user.id, fresh_exercise.id)
    legacy = UserExerciseProgressionState(app_user_id=test_user.id, exercise_id=fresh_exercise.id,
        records={'legacy': {'e1rm': 123.456}})
    report = PeriodReport(app_user_id=test_user.id, period_type='week', period_start=date(2025,1,1),
        period_end=date(2025,1,7), payload={'original': {'value': 123.456, 'formula': 'old'}})
    event = UserRecord(app_user_id=test_user.id, exercise_id=fresh_exercise.id,
        exercise_name='Historical lift', record_type='max_weight', value=123.456, date_achieved=date(2025,1,1))
    db.add_all([legacy, report, event])
    await db.commit()
    notifications = await db.scalar(select(func.count()).select_from(AppNotification).where(AppNotification.app_user_id == test_user.id))
    await rebuild.rebuild_user(db, test_user.id)
    await rebuild.rebuild_user(db, test_user.id)
    await db.refresh(legacy)
    await db.refresh(report)
    assert legacy.records == {'legacy': {'e1rm': 123.456}}
    assert report.payload == {'original': {'value': 123.456, 'formula': 'old'}}
    await db.refresh(event)
    assert event.value == 123.456 and event.date_achieved == date(2025,1,1)
    assert await db.scalar(select(func.count()).select_from(AppNotification).where(AppNotification.app_user_id == test_user.id)) == notifications


@pytest.mark.asyncio
async def test_initial_reconstruction_freezes_unknown_and_retains_counts(db, test_user, fresh_exercise):
    rebuild = implementation()
    rows = await history(db, test_user.id, fresh_exercise.id)
    initial = await rebuild.rebuild_user(db, test_user.id, reconstruct_legacy=True)
    assert initial['reconstructed'] == 3 and initial['missing_body_mass'] == 3
    db.add(UserAnthropometry(app_user_id=test_user.id, weight=80, measured_on=date(2020,1,1), submitted_fields=['weight']))
    await mark_dirty(db, test_user.id)
    await db.commit()
    await rebuild.rebuild_user(db, test_user.id, reconstruct_legacy=True)
    for workout_id, _ in rows:
        snapshot = await db.scalar(select(WorkoutSession.analytics_snapshot).where(WorkoutSession.id == workout_id))
        assert snapshot is not None and snapshot['body_mass'] is None


@pytest.mark.asyncio
async def test_all_unknown_and_gym_scopes_and_removed_exercise(db, test_user, fresh_exercise):
    rebuild = implementation()
    rows = await history(db, test_user.id, fresh_exercise.id)
    gym = uuid4()
    await db.execute(update(WorkoutSessionSet).where(WorkoutSessionSet.id == rows[-1][1]).values(gym_profile_id=gym, setup_id=uuid4()))
    await db.commit()
    await rebuild.rebuild_user(db, test_user.id)
    assert (await rebuild.active_records(db, test_user.id, fresh_exercise.id, 'unknown'))['best']['weight'] == 110
    assert (await rebuild.active_records(db, test_user.id, fresh_exercise.id, f'gym:{gym}'))['best']['weight'] == 120
    await db.execute(delete(WorkoutSessionSet).where(WorkoutSessionSet.id.in_([r[1] for r in rows])))
    await mark_dirty(db, test_user.id)
    await db.commit()
    await rebuild.rebuild_user(db, test_user.id)
    assert await rebuild.active_records(db, test_user.id, fresh_exercise.id, 'all') is None


@pytest.mark.asyncio
async def test_initial_only_reconstruction_cannot_backfill_after_publication(db, test_user, fresh_exercise):
    rebuild = implementation()
    rows = await history(db, test_user.id, fresh_exercise.id)
    initial = await rebuild.rebuild_user(db, test_user.id)
    assert initial['missing_snapshots'] == 3
    db.add(UserAnthropometry(app_user_id=test_user.id, weight=80, measured_on=date(2020,1,1), submitted_fields=['weight']))
    await mark_dirty(db, test_user.id)
    await db.commit()
    later = await rebuild.rebuild_user(db, test_user.id, reconstruct_legacy=True)
    assert later['reconstructed'] == 0 and later['missing_snapshots'] == 3
    for workout_id, _ in rows:
        assert await db.scalar(select(WorkoutSession.analytics_snapshot).where(WorkoutSession.id == workout_id)) is None


@pytest.mark.asyncio
async def test_worker_lease_prevents_concurrent_generation_deletion(db, test_user, fresh_exercise, monkeypatch):
    rebuild = implementation()
    await history(db, test_user.id, fresh_exercise.id)
    original = rebuild._publish
    async def compete(*args, **kwargs):
        async with SessionLocal() as other:
            assert (await rebuild.rebuild_user(other, test_user.id))['status'] == 'busy'
        return await original(*args, **kwargs)
    monkeypatch.setattr(rebuild, '_publish', compete)
    result = await rebuild.rebuild_user(db, test_user.id)
    assert result['status'] == 'ready'
    assert len((await rebuild.active_records(db, test_user.id, fresh_exercise.id, 'all'))['events']) == 2


@pytest.mark.asyncio
async def test_reconstruction_uses_historical_measurement_and_persists_source(db, test_user, fresh_exercise):
    rebuild = implementation()
    await history(db, test_user.id, fresh_exercise.id)
    db.add_all([
        UserAnthropometry(app_user_id=test_user.id, weight=70, measured_on=date(2020,1,1), submitted_fields=['weight']),
        UserAnthropometry(app_user_id=test_user.id, weight=95, measured_on=date(2099,1,1), submitted_fields=['weight']),
        UserAnthropometry(app_user_id=test_user.id, weight=99, measured_on=date(2021,1,1), submitted_fields=['height']),
    ])
    await db.commit()
    result = await rebuild.rebuild_user(db, test_user.id, reconstruct_legacy=True)
    payload = await rebuild.active_records(db, test_user.id, fresh_exercise.id, 'all')
    assert result['reconstructed'] == 3 and result['missing_body_mass'] == 0
    assert payload['initial']['session_context']['body_mass']['weight_kg'] == 70
    assert payload['initial']['session_context']['body_mass']['source'] == 'reconstructed'


@pytest.mark.asyncio
async def test_cli_refuses_implicit_database_and_defaults_readonly(db, test_user, fresh_exercise):
    import asyncio
    import os
    import sys
    import json
    await history(db, test_user.id, fresh_exercise.id)
    command = [sys.executable, 'scripts/rebuild_analytics.py', '--user-id', str(test_user.id)]
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await process.communicate()
    assert process.returncode == 0 and json.loads(out)['status'] == 'dry_run'
    state = await read_state(db, test_user.id)
    assert state.active_generation is None
    environment = dict(os.environ)
    environment.pop('DATABASE_URL', None)
    process = await asyncio.create_subprocess_exec(*command, '--apply', env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await process.communicate()
    assert process.returncode == 1 and not out
    assert json.loads(err) == {'status':'failed','error':'ValueError'}
