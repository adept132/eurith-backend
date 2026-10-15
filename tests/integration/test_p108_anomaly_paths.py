from copy import deepcopy

import pytest
from sqlalchemy import select
from api.services.models import WorkoutSessionSet, AnalyticsState
from tests.integration.test_p108_write_paths import snapshot, push
from tests.integration.test_workout_history_api import history_exercise, history_body

pytestmark = pytest.mark.asyncio
LOAD = {'schema_version': 1, 'model': 'barbell', 'input_basis': 'full', 'rep_basis': 'simultaneous'}


@pytest.mark.parametrize('reps', [301, 1001])
async def test_high_reps_all_write_paths(client, db, test_user, seeded_history, history_exercise, monkeypatch, reps):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    data['exercises'][0]['analytics_load_defaults'] = LOAD
    data['exercises'][0]['sets'][0].update(weight=20, reps=reps)
    saved = await push(client, data)
    ex = saved['workout']['exercises'][0]
    assert ex['sets'][0]['is_anomalous'] is False
    added = await client.post(f"/workout-session-exercises/{ex['id']}/sets", json={'weight': 20, 'reps': reps})
    assert added.status_code == 200, added.text
    sid = added.json()['id']
    patched = await client.patch(f'/workout-session-sets/{sid}', json={'reps': reps})
    assert patched.status_code == 200, patched.text
    assert patched.json()['is_anomalous'] is False
    repeated = await client.post(f'/workout-session-sets/{sid}/repeat')
    assert repeated.status_code == 200, repeated.text
    body = history_body(history_exercise.id)
    body['exercises'][0]['sets'][0].update(reps=reps, analytics_load_snapshot=LOAD)
    created = await client.post('/workouts/history', json=body)
    assert created.status_code == 201, created.text
    wid = created.json()['workout']['id']
    draft = await client.get(f'/workouts/history/{wid}/draft')
    assert draft.status_code == 200, draft.text
    assert draft.json()['exercises'][0]['sets'][0]['reps'] == reps


@pytest.mark.parametrize('weight', [750, 2500])
async def test_heavy_confirmable(client, db, test_user, seeded_history, monkeypatch, weight):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    data['exercises'][0]['analytics_load_defaults'] = LOAD
    data['exercises'][0]['sets'][0].update(weight=weight, reps=8, anomaly_confirmed=True)
    saved = await push(client, data)
    row = saved['workout']['exercises'][0]['sets'][0]
    assert row['is_anomalous'] is False
    assert row['anomaly_review']['confirmed'] is True
    revision = await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id))
    await push(client, data)
    assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision


async def test_legacy_flag_without_reason_remains_unknown(client, db, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    data['exercises'][0].pop('analytics_load_defaults')
    saved = await push(client, data)
    sid = saved['workout']['exercises'][0]['sets'][0]['id']
    row = await db.get(WorkoutSessionSet, sid)
    row.is_anomalous = True
    row.anomaly_review = None
    await db.commit()
    response = await client.patch(f'/workout-session-sets/{sid}', json={'reps': 301})
    assert response.status_code == 200, response.text
    assert response.json()['is_anomalous'] is True
    assert response.json()['anomaly_review']['reason_codes'] == ['legacy_unknown']


async def test_history_context_recomputes_and_correction_replays(client, db, test_user, history_exercise, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    body = history_body(history_exercise.id, weight='500')
    body['exercises'][0]['sets'][0]['analytics_load_snapshot'] = LOAD
    created = await client.post('/workouts/history', json=body)
    wid = created.json()['workout']['id']
    draft = (await client.get(f'/workouts/history/{wid}/draft')).json()
    draft['exercises'][0].pop('exercise_name')
    draft['exercises'][0]['sets'][0]['analytics_load_snapshot'].update(input_basis='plates_only', base_weight_kg=200)
    changed = await client.patch(f'/workouts/{wid}/history', json=draft)
    assert changed.status_code == 200, changed.text
    row = changed.json()['workout']['exercises'][0]['sets'][0]
    assert row['is_anomalous'] is True
    assert row['anomaly_review']['reason_codes'] == ['unusual_weight']
    revision = await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id))
    replay = await client.patch(f'/workouts/{wid}/history', json=draft)
    assert replay.status_code == 200, replay.text
    assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision


async def test_import_1001_and_confirmed_2500(client, db, test_user, history_exercise, monkeypatch):
    from tests.integration.test_p108_history_io import csv_rows, csv_text
    import json
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    rows = csv_rows()
    rows[0].update({'Exercise Name': history_exercise.name, 'Reps': '1001', 'Weight': '2500',
                   'analytics_load_snapshot_json': json.dumps(LOAD)})
    payload = {'csv': csv_text(rows), 'unit': 'kg'}
    response = await client.post('/data/import/commit', json=payload)
    assert response.status_code == 200, response.text
    row = await db.scalar(select(WorkoutSessionSet).where(WorkoutSessionSet.reps == 1001))
    assert row.weight == 2500 and not row.is_anomalous
    assert row.anomaly_review['confirmed']
    revision = await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id))
    replay = await client.post('/data/import/commit', json=payload)
    assert replay.status_code == 200, replay.text
    assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision


async def test_confirmation_revocation_and_noop(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    data['exercises'][0]['analytics_load_defaults'] = LOAD
    data['exercises'][0]['sets'][0].update(weight=750, reps=8, anomaly_confirmed=True)
    saved = await push(client, data)
    sid = saved['workout']['exercises'][0]['sets'][0]['id']
    revision = await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id))
    revoked = await client.patch(f'/workout-session-sets/{sid}', json={'anomaly_confirmed': False})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()['is_anomalous'] is True
    assert revoked.json()['anomaly_review']['confirmed'] is False
    assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision + 1
    replay = await client.patch(f'/workout-session-sets/{sid}', json={'anomaly_confirmed': False})
    assert replay.status_code == 200, replay.text
    assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision + 1


async def test_history_confirmation_is_semantic_and_replayable(client, db, test_user, history_exercise, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    body = history_body(history_exercise.id, weight='750')
    body['exercises'][0]['sets'][0]['analytics_load_snapshot'] = LOAD
    created = await client.post('/workouts/history', json=body)
    wid = created.json()['workout']['id']
    draft = (await client.get(f'/workouts/history/{wid}/draft')).json()
    draft['exercises'][0].pop('exercise_name')
    draft['exercises'][0]['sets'][0]['anomaly_review']['confirmed'] = True
    first = await client.patch(f'/workouts/{wid}/history', json=draft)
    assert first.status_code == 200, first.text
    assert not first.json()['workout']['exercises'][0]['sets'][0]['is_anomalous']
    revision = await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id))
    replay = await client.patch(f'/workouts/{wid}/history', json=draft)
    assert replay.status_code == 200, replay.text
    assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision


async def test_sync_full_review_confirmation_replay(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    data = snapshot(seeded_history.id)
    data['exercises'][0]['analytics_load_defaults'] = LOAD
    data['exercises'][0]['sets'][0].update(weight=750, reps=8, anomaly_review={
        'rules_version': 'p1-08-v1', 'reason_codes': ['unusual_weight'], 'confirmed': True})
    first = await push(client, data)
    revision = await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id))
    second = await push(client, data)
    assert first['workout']['exercises'][0]['sets'][0]['anomaly_review'] == second['workout']['exercises'][0]['sets'][0]['anomaly_review']
    assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision


async def test_disabled_confirmation_revocation_and_exact_replay(client, db, test_user, seeded_history, monkeypatch):
    monkeypatch.delenv('P108_ANALYTICS_ENABLED', raising=False)
    data = snapshot(seeded_history.id)
    data['exercises'][0].pop('analytics_load_defaults')
    data['exercises'][0]['sets'][0].update(weight=750, reps=8)
    first = await push(client, data)
    row = first['workout']['exercises'][0]['sets'][0]
    assert row['is_anomalous'] is True
    assert row['anomaly_review']['reason_codes'] == ['unusual_weight']
    sid = row['id']
    revision = await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id))
    await push(client, data)
    assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision
    for confirmed in (True, False):
        changed = await client.patch(f'/workout-session-sets/{sid}', json={'anomaly_confirmed': confirmed})
        assert changed.status_code == 200, changed.text
        assert changed.json()['is_anomalous'] is not confirmed
        assert changed.json()['anomaly_review']['confirmed'] is confirmed
        revision += 1
        assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision
        replay = await client.patch(f'/workout-session-sets/{sid}', json={'anomaly_confirmed': confirmed})
        assert replay.status_code == 200, replay.text
        assert replay.json()['anomaly_review'] == changed.json()['anomaly_review']
        assert await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == test_user.id)) == revision


async def test_disabled_confirmation_preserves_genuine_legacy_unknown(client, db, seeded_history, monkeypatch):
    monkeypatch.delenv('P108_ANALYTICS_ENABLED', raising=False)
    data = snapshot(seeded_history.id)
    data['exercises'][0].pop('analytics_load_defaults')
    first = await push(client, data)
    sid = first['workout']['exercises'][0]['sets'][0]['id']
    row = await db.get(WorkoutSessionSet, sid)
    row.is_anomalous, row.anomaly_review = True, None
    await db.commit()
    changed = await client.patch(f'/workout-session-sets/{sid}', json={'anomaly_confirmed': True})
    assert changed.status_code == 200, changed.text
    assert changed.json()['is_anomalous'] is True
