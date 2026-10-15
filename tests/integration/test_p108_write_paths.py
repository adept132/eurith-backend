from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from api.services.models import AnalyticsState, UserAnthropometry

pytestmark = pytest.mark.asyncio


def snapshot(exercise_id):
    return {'client_uuid': str(uuid4()), 'source': 'free', 'status': 'active',
            'started_at': datetime.now(timezone.utc).isoformat(), 'exercises': [
                {'client_uuid': str(uuid4()), 'exercise_id': exercise_id, 'order_index': 0,
                 'analytics_load_defaults': {'model': 'bodyweight', 'input_basis': 'body_components',
                                             'added_load_kg': 5, 'assistance_kg': 0},
                 'sets': [{'client_uuid': str(uuid4()), 'set_number': 1, 'reps': 8}]}]}


async def push(client, data):
    response = await client.post('/sync/workouts', json=data)
    assert response.status_code == 200, response.text
    return response.json()


async def test_start_add_repeat_finish_roundtrip(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    data['exercises'][0]['superset_group'] = str(uuid4())
    saved = await push(client, data)
    row = saved['workout']['exercises'][0]
    flow = await client.get(f"/workout-supersets/{data['exercises'][0]['superset_group']}")
    assert flow.status_code == 200, flow.text
    assert flow.json()['analytics_snapshot'] == saved['workout']['analytics_snapshot']
    assert flow.json()['exercises'][0]['analytics_load_defaults'] == row['analytics_load_defaults']
    assert flow.json()['exercises'][0]['sets'][0]['analytics_load_snapshot'] == row['sets'][0]['analytics_load_snapshot']
    assert row['sets'][0]['analytics_load_snapshot']['added_load_kg'] == 5
    added = await client.post(f"/workout-session-exercises/{row['id']}/sets", json={'reps': 9})
    assert added.status_code == 200, added.text
    repeated = await client.post(f"/workout-session-sets/{added.json()['id']}/repeat")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['client_uuid'] != added.json()['client_uuid']
    assert repeated.json()['analytics_load_snapshot'] == added.json()['analytics_load_snapshot']
    detail = await client.get(f"/workouts/{saved['workout']['id']}")
    assert detail.json()['analytics_snapshot']['schema_version'] == 1
    finished = await client.post(f"/workouts/{saved['workout']['id']}/finish")
    assert finished.status_code == 200, finished.text
    assert finished.json()['analytics_snapshot'] == detail.json()['analytics_snapshot']


async def test_sync_replay_has_one_mass_and_revision(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    data['status'] = 'finished'
    data['finished_at'] = datetime.now(timezone.utc).isoformat()
    data['analytics_mass_intent'] = {'client_uuid': str(uuid4()), 'weight_kg': 80,
                                    'measured_on': datetime.now(timezone.utc).date().isoformat()}
    data['analytics_snapshot'] = {'schema_version': 1, 'body_mass': {'schema_version': 1,
        'weight_kg': 80, 'measured_on': data['analytics_mass_intent']['measured_on'],
        'source': 'manual', 'captured_at': datetime.now(timezone.utc).isoformat()},
        'is_deload': None, 'phase_source': None}
    first = await push(client, data)
    revision = await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id))
    second = await push(client, data)
    assert first['workout']['analytics_snapshot'] == second['workout']['analytics_snapshot']
    assert await db.scalar(select(func.count()).select_from(UserAnthropometry).where(
        UserAnthropometry.client_uuid == data['analytics_mass_intent']['client_uuid'])) == 1
    assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision


async def test_legacy_patch_preserves_context(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    saved = await push(client, snapshot(seeded_history.id))
    row = saved['workout']['exercises'][0]['sets'][0]
    response = await client.patch(f"/workout-session-sets/{row['id']}", json={'notes': 'ok'})
    assert response.status_code == 200, response.text
    assert response.json()['analytics_load_snapshot'] == row['analytics_load_snapshot']
    response = await client.patch(f"/workout-session-sets/{row['id']}", json={'weight': 10})
    assert response.status_code == 409


async def test_owner_and_version_conflict(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    saved = await push(client, snapshot(seeded_history.id))
    route = f"/workouts/{saved['workout']['id']}/analytics-context"
    response = await client.patch(route, json={'base_sync_version': 999, 'load_defaults': {}})
    assert response.status_code == 409
    response = await client.patch('/workouts/2147483647/analytics-context', json={'base_sync_version': 0})
    assert response.status_code == 404


async def test_default_change_after_first_set(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    saved = await push(client, snapshot(seeded_history.id))
    workout = saved['workout']; exercise = workout['exercises'][0]
    defaults = deepcopy(exercise['analytics_load_defaults']); defaults['added_load_kg'] = 20
    response = await client.patch(f"/workouts/{workout['id']}/analytics-context", json={
        'base_sync_version': saved['sync_version'], 'load_defaults': {str(exercise['id']): defaults}})
    assert response.status_code == 200, response.text
    added = await client.post(f"/workout-session-exercises/{exercise['id']}/sets", json={'reps': 9})
    assert added.status_code == 200, added.text
    assert added.json()['analytics_load_snapshot']['added_load_kg'] == 20
    assert response.json()['exercises'][0]['sets'][0]['analytics_load_snapshot']['added_load_kg'] == 5


async def test_disabled_preserves_existing_and_rejects_new_context(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    saved = await push(client, data)
    monkeypatch.delenv('P108_ANALYTICS_ENABLED')
    replay = await push(client, data)
    assert replay['workout']['analytics_snapshot'] == saved['workout']['analytics_snapshot']
    data['exercises'][0]['analytics_load_defaults']['added_load_kg'] = 30
    response = await client.post('/sync/workouts', json=data)
    assert response.status_code == 409
    row = saved['workout']['exercises'][0]['sets'][0]
    response = await client.patch(f"/workout-session-sets/{row['id']}", json={'notes': 'legacy'})
    assert response.status_code == 200
    assert response.json()['analytics_load_snapshot'] == row['analytics_load_snapshot']


async def test_future_mass_rolls_back_and_snapshot_is_not_weighing(client, db, test_user, seeded_history, monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    weighing = {'client_uuid': str(uuid4()), 'weight_kg': 81,
                'measured_on': (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat()}
    data['analytics_mass_intent'] = weighing
    response = await client.post('/sync/workouts', json=data)
    assert response.status_code == 422
    assert await db.scalar(select(func.count()).select_from(UserAnthropometry).where(
        UserAnthropometry.client_uuid == weighing['client_uuid'])) == 0
    del data['analytics_mass_intent']
    data['analytics_snapshot'] = {'body_mass': {'weight_kg': 80, 'source': 'manual',
        'measured_on': datetime.now(timezone.utc).date().isoformat(),
        'captured_at': datetime.now(timezone.utc).isoformat()}}
    await push(client, data)
    assert await db.scalar(select(func.count()).select_from(UserAnthropometry).where(
        UserAnthropometry.app_user_id == test_user.id)) == 0


async def test_versioned_unknown_and_omitted_null(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    data['analytics_snapshot'] = {'schema_version': 1, 'body_mass': None, 'is_deload': None}
    saved = await push(client, data)
    legacy = deepcopy(data)
    del legacy['analytics_snapshot']; del legacy['exercises'][0]['analytics_load_defaults']
    replay = await push(client, legacy)
    assert replay['workout']['analytics_snapshot'] == saved['workout']['analytics_snapshot']
    legacy['exercises'][0]['sets'][0]['analytics_load_snapshot'] = None
    replay = await push(client, legacy)
    assert replay['workout']['exercises'][0]['sets'][0]['analytics_load_snapshot'] is None


async def test_center_start_capture_and_superset_serializers(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    started = await client.post('/workouts/start', json={'source': 'free'})
    assert started.status_code == 200, started.text
    assert started.json()['analytics_snapshot']['schema_version'] == 1
    workout_id = started.json()['id']
    added = await client.post(f'/workouts/{workout_id}/exercises', json={
        'exercise_id': seeded_history.id,
        'analytics_load_defaults': {'model': 'stack', 'input_basis': 'displayed'}})
    assert added.status_code == 200, added.text
    exercise = added.json()['exercises'][0]
    row = await client.post(f"/workout-session-exercises/{exercise['id']}/sets", json={'weight': 20, 'reps': 8})
    assert row.status_code == 200, row.text
    from api.services.workout_superset_service import WorkoutSupersetService
    from api.services.models import WorkoutSessionSet
    from api.schemas.supersets import SupersetWorkoutSetItem
    stored = await db.get(WorkoutSessionSet, row.json()['id'])
    serialized = SupersetWorkoutSetItem.model_validate(WorkoutSupersetService._serialize_set(stored))
    assert serialized.analytics_load_snapshot['model'] == 'stack'


async def test_routine_start_captures_snapshot(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    created = await client.post('/workout-routines', json={'client_uuid': str(uuid4()), 'name': 'Capture',
        'exercises': [{'exercise_id': seeded_history.id, 'order_index': 0, 'target_sets': 3,
                       'set_kinds': ['normal', 'normal', 'normal']}]})
    assert created.status_code == 201, created.text
    response = await client.post(f"/workout-routines/{created.json()['id']}/start")
    assert response.status_code == 201, response.text
    assert response.json()['analytics_snapshot']['schema_version'] == 1


async def test_foreign_mass_reference_rejected_and_sync_legacy_weight_conflicts(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    saved = await push(client, data)
    data['exercises'][0]['sets'][0]['weight'] = 10
    response = await client.post('/sync/workouts', json=data)
    assert response.status_code == 409
    data['exercises'][0]['sets'][0].pop('weight')
    data['analytics_snapshot'] = {'body_mass': {'weight_kg': 80, 'source': 'journal',
        'measurement_id': 2147483647, 'measured_on': datetime.now(timezone.utc).date().isoformat(),
        'captured_at': datetime.now(timezone.utc).isoformat()}}
    response = await client.post('/sync/workouts', json=data)
    assert response.status_code == 409
    detail = await client.get(f"/workouts/{saved['workout']['id']}")
    assert detail.json()['analytics_snapshot'] == saved['workout']['analytics_snapshot']


async def test_actual_foreign_workout_and_measurement_ids(client, db, test_user, seeded_history, monkeypatch):
    from sqlalchemy import delete
    from api.services.models import AppUser, WorkoutSession
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    foreign = AppUser(firebase_uid=f'foreign-{uuid4()}', email=f'{uuid4()}@example.com')
    db.add(foreign)
    await db.flush()
    foreign_id = foreign.id
    mass = UserAnthropometry(app_user_id=foreign_id, weight=80,
        measured_on=datetime.now(timezone.utc).date(), submitted_fields=['weight'])
    workout = WorkoutSession(app_user_id=foreign_id, source='free', status='active',
                             started_at=datetime.now(timezone.utc))
    db.add_all([mass, workout]); await db.commit()
    mass_id, workout_id = mass.id, workout.id
    try:
        response = await client.patch(f'/workouts/{workout_id}/analytics-context', json={'base_sync_version': 0})
        assert response.status_code == 404
        data = snapshot(seeded_history.id); data['server_id'] = workout_id
        response = await client.post('/sync/workouts', json=data)
        assert response.status_code == 404
        data.pop('server_id')
        data['analytics_snapshot'] = {'body_mass': {'weight_kg': 80, 'source': 'journal',
            'measurement_id': mass_id, 'measured_on': datetime.now(timezone.utc).date().isoformat(),
            'captured_at': datetime.now(timezone.utc).isoformat()}}
        response = await client.post('/sync/workouts', json=data)
        assert response.status_code == 409
    finally:
        await db.execute(delete(WorkoutSession).where(WorkoutSession.app_user_id == foreign_id))
        await db.execute(delete(UserAnthropometry).where(UserAnthropometry.app_user_id == foreign_id))
        await db.execute(delete(AppUser).where(AppUser.id == foreign_id))
        await db.commit()
