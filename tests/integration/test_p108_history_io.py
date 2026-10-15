"""History corrections and full CSV carry durable, owner-safe analytics facts."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select, func

from api.services.csv_import import parse_csv, CsvParseError
from api.services.csv_export import iter_csv, columns_for
from api.services.models import AnalyticsState, UserAnthropometry, WorkoutSessionSet
from tests.integration.test_workout_history_api import history_exercise, history_body


MASS = {'schema_version': 1, 'body_mass': {'schema_version': 1, 'weight_kg': 75,
    'measured_on': '2026-08-20', 'source': 'manual', 'captured_at': '2026-08-20T17:00:00Z'}}
LOAD = {'schema_version': 1, 'model': 'bodyweight', 'input_basis': 'body_components',
        'added_load_kg': 0, 'assistance_kg': 10}
REVIEW = {'rules_version': 'p1-08-v1', 'reason_codes': ['reviewed'], 'confirmed': True,
          'confirmed_at': '2026-08-20T17:00:00Z'}


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')


async def revision(db, user_id):
    return await db.scalar(select(AnalyticsState.source_revision).where(AnalyticsState.app_user_id == user_id))


@pytest.mark.asyncio
async def test_history_context_revision(client, db, test_user, history_exercise):
    body = history_body(history_exercise.id)
    body['analytics_snapshot'] = deepcopy(MASS)
    body['exercises'][0]['analytics_load_defaults'] = LOAD
    body['exercises'][0]['sets'][0]['analytics_load_snapshot'] = LOAD
    created = await client.post('/workouts/history', json=body)
    assert created.status_code == 201, created.text
    wid = created.json()['workout']['id']
    before = await revision(db, test_user.id)
    snapshot = await client.get(f'/workouts/history/{wid}/draft')
    assert snapshot.status_code == 200, snapshot.text
    draft = snapshot.json()
    draft['exercises'][0].pop('exercise_name')
    draft['analytics_snapshot']['body_mass']['weight_kg'] = 77
    corrected = await client.patch(f'/workouts/{wid}/history', json=draft)
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()['workout']['revision'] == 1
    assert await revision(db, test_user.id) == before + 1
    assert await db.scalar(select(func.count()).select_from(UserAnthropometry).where(UserAnthropometry.app_user_id == test_user.id)) == 0
    replay = await client.patch(f'/workouts/{wid}/history', json=draft)
    assert replay.status_code == 200, replay.text
    assert await revision(db, test_user.id) == before + 1
    draft['analytics_snapshot']['body_mass']['weight_kg'] = 79
    stale = await client.patch(f'/workouts/{wid}/history', json=draft)
    assert stale.status_code == 409
    assert stale.json()['detail']['server_draft']['analytics_snapshot']['body_mass']['weight_kg'] == 77


@pytest.mark.asyncio
async def test_context_only_edit_preserves_unrelated_flags(client, db, history_exercise):
    from tests.integration.test_sync_warmup_snapshots import group_fixture, row_fixture
    body = history_body(history_exercise.id)
    group = group_fixture()
    body['exercises'][0]['warmup_group'] = group
    body['exercises'][0]['order_index'] = 4
    body['exercises'][0]['sets'][0].update(set_type='warmup', warmup_snapshot=row_fixture(group))
    created = await client.post('/workouts/history', json=body)
    assert created.status_code == 201, created.text
    wid = created.json()['workout']['id']
    sid = created.json()['workout']['exercises'][0]['sets'][0]['id']
    row = await db.get(WorkoutSessionSet, sid)
    row.is_max_reps = True
    row.is_anomalous = True
    row.shown_target_snapshot = {'target_reps': 9}
    row.superset_round = 3
    row.anomaly_review = REVIEW
    await db.commit()
    body['analytics_snapshot'] = MASS
    result = await client.patch(f'/workouts/{wid}/history', json=body)
    assert result.status_code == 200, result.text
    await db.refresh(row)
    assert row.is_max_reps is True
    assert row.is_anomalous is True
    assert row.shown_target_snapshot == {'target_reps': 9}
    assert row.superset_round == 3
    assert row.anomaly_review == REVIEW
    assert row.weight == 80 and row.reps == 8 and row.notes == 'Set note'
    assert row.warmup_snapshot == row_fixture(group)
    fetched = await client.get(f'/workouts/history/{wid}/draft')
    assert fetched.json()['exercises'][0]['sets'][0]['is_max_reps'] is True
    assert fetched.json()['exercises'][0]['order_index'] == 4
    assert fetched.json()['exercises'][0]['warmup_group'] == group


def csv_rows():
    return [{'Date': '2026-08-20 18:00:00', 'Workout Name': 'Workout',
             'Exercise Name': 'Pullup', 'Set Order': '1', 'Weight': '0', 'Reps': '8',
             'Duration': '1h', 'workout_source_key': 'session-a',
             'exercise_instance_source_key': 'exercise-a',
             'analytics_snapshot_json': json.dumps(MASS),
             'analytics_load_defaults_json': json.dumps(LOAD),
             'analytics_load_snapshot_json': json.dumps(LOAD),
             'anomaly_review_json': json.dumps(REVIEW)}]


def csv_text(rows):
    # Explicit headers also exercise parser RED before export supports extensions.
    return ''.join(iter_csv(rows, list(rows[0])))


def test_full_csv_context_roundtrip():
    rows = csv_rows()
    parsed = parse_csv(csv_text(rows))
    workout = parsed.workouts[0]
    assert workout.analytics_snapshot['body_mass']['weight_kg'] == 75
    assert workout.exercises[0].analytics_load_defaults['assistance_kg'] == 10
    assert workout.exercises[0].sets[0].analytics_load_snapshot['assistance_kg'] == 10
    assert workout.exercises[0].sets[0].anomaly_review == REVIEW
    assert 'analytics_snapshot_json' in columns_for('full')
    assert 'analytics_snapshot_json' not in columns_for('strong')


def test_legacy_csv_is_unknown():
    parsed = parse_csv('Date,Exercise Name,Set Order,Weight,Reps\n2026-08-20,Pullup,1,0,8\n')
    assert parsed.workouts[0].analytics_snapshot is None
    assert parsed.workouts[0].exercises[0].analytics_load_defaults is None
    assert parsed.workouts[0].exercises[0].sets[0].analytics_load_snapshot is None


def test_csv_separate_instances_and_sessions_and_reject_contradictions():
    rows = csv_rows()
    other = deepcopy(rows[0])
    other['exercise_instance_source_key'] = 'exercise-b'
    other['analytics_load_defaults_json'] = json.dumps({**LOAD, 'assistance_kg': 20})
    rows.append(other)
    third = deepcopy(rows[0])
    third['workout_source_key'] = 'session-b'
    rows.append(third)
    parsed = parse_csv(csv_text(rows))
    assert len(parsed.workouts) == 2
    assert len(parsed.workouts[0].exercises) == 2
    assert parsed.workouts[0].import_key != parsed.workouts[1].import_key
    contradiction = deepcopy(rows[0])
    contradiction['Set Order'] = '2'
    contradiction['analytics_snapshot_json'] = json.dumps({**MASS, 'is_deload': True})
    with pytest.raises(CsvParseError, match='[Cc]ontradict'):
        parse_csv(csv_text([rows[0], contradiction]))


@pytest.mark.asyncio
async def test_import_provenance_idempotency_and_outer_dirty(client, db, test_user, history_exercise):
    rows = csv_rows()
    mass = deepcopy(MASS)
    mass['body_mass'].update(source='journal', measurement_id=987654, measurement_client_uuid='foreign')
    rows[0]['analytics_snapshot_json'] = json.dumps(mass)
    rows[0]['Exercise Name'] = history_exercise.name
    another_instance = deepcopy(rows[0])
    another_instance['exercise_instance_source_key'] = 'exercise-b'
    another_instance['analytics_load_defaults_json'] = json.dumps({**LOAD, 'assistance_kg': 20})
    rows.append(another_instance)
    another_session = deepcopy(rows[0])
    another_session['workout_source_key'] = 'session-b'
    rows.append(another_session)
    payload = {'csv': csv_text(rows), 'unit': 'kg'}
    preview = await client.post('/data/import/preview', json=payload)
    assert preview.status_code == 200, preview.text
    first = await client.post('/data/import/commit', json=payload)
    assert first.status_code == 200, first.text
    assert first.json()['added_workouts'] == 2
    assert await revision(db, test_user.id) == 1
    second = await client.post('/data/import/commit', json=payload)
    assert second.status_code == 200, second.text
    assert second.json()['skipped_duplicates'] == 2
    assert await revision(db, test_user.id) == 1
    exported = await client.get('/data/export?format=full')
    parsed = parse_csv(exported.text)
    assert len(parsed.workouts) == 2
    assert sorted(len(w.exercises) for w in parsed.workouts) == [1, 2]
    assert sorted(e.analytics_load_defaults['assistance_kg'] for w in parsed.workouts for e in w.exercises) == [10, 10, 20]
    for workout in parsed.workouts:
        assert workout.analytics_snapshot['body_mass']['weight_kg'] == 75
        for exercise in workout.exercises:
            for row in exercise.sets:
                assert row.analytics_load_snapshot['assistance_kg'] == 10
                assert row.anomaly_review == REVIEW
    imported = parsed.workouts[0].analytics_snapshot['body_mass']
    assert imported['source'] == 'imported'
    assert imported['measurement_id'] is None and imported['measurement_client_uuid'] is None
    assert imported['import_provenance']['original_measurement_id'] == '987654'
    assert imported['import_provenance']['original_source'] == 'journal'
    assert await db.scalar(select(func.count()).select_from(UserAnthropometry).where(UserAnthropometry.app_user_id == test_user.id)) == 0


@pytest.mark.asyncio
async def test_draft_is_one_joined_owner_read(client, db, history_exercise, test_user):
    from sqlalchemy import event
    from api.services.workout_history import get_history_draft, WorkoutHistoryNotFound
    created = await client.post('/workouts/history', json=history_body(history_exercise.id))
    wid = created.json()['workout']['id']
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    bind = db.bind.sync_engine
    event.listen(bind, 'before_cursor_execute', record)
    try:
        draft = await get_history_draft(db, test_user.id, wid)
    finally:
        event.remove(bind, 'before_cursor_execute', record)
    assert draft.base_revision == 0 and draft.exercises[0].sets[0].reps == 8
    assert len(statements) == 1
    assert 'JOIN workout_session_sets' in statements[0]
    with pytest.raises(WorkoutHistoryNotFound):
        await get_history_draft(db, -1, wid)


@pytest.mark.asyncio
async def test_history_gate_noop_and_owner_link_validation(client, db, test_user, history_exercise, monkeypatch):
    body = history_body(history_exercise.id)
    body['analytics_snapshot'] = deepcopy(MASS)
    created = await client.post('/workouts/history', json=body)
    wid = created.json()['workout']['id']
    assert await revision(db, test_user.id) == 1
    unchanged = await client.patch(f'/workouts/{wid}/history', json=body)
    assert unchanged.status_code == 200
    assert unchanged.json()['workout']['revision'] == 0
    assert await revision(db, test_user.id) == 1
    monkeypatch.delenv('P108_ANALYTICS_ENABLED')
    body['notes'] = 'legacy edit'
    body.pop('analytics_snapshot')
    legacy = await client.patch(f'/workouts/{wid}/history', json=body)
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()['workout']['analytics_snapshot']['body_mass']['weight_kg'] == 75
    assert await revision(db, test_user.id) == 2
    body['base_revision'] = 1
    body['analytics_snapshot'] = deepcopy(MASS)
    body['analytics_snapshot']['body_mass']['weight_kg'] = 80
    blocked = await client.patch(f'/workouts/{wid}/history', json=body)
    assert blocked.status_code == 409
    assert await revision(db, test_user.id) == 2
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    body['analytics_snapshot']['body_mass'].update(source='journal', measurement_id=987654)
    rejected = await client.patch(f'/workouts/{wid}/history', json=body)
    assert rejected.status_code == 409
    assert await revision(db, test_user.id) == 2


@pytest.mark.asyncio
async def test_disabled_import_rejects_context_but_accepts_legacy(client, db, test_user, history_exercise, monkeypatch):
    monkeypatch.delenv('P108_ANALYTICS_ENABLED')
    rows = csv_rows()
    rows[0]['Exercise Name'] = history_exercise.name
    response = await client.post('/data/import/commit', json={'csv': csv_text(rows)})
    assert response.status_code == 409, response.text
    assert await revision(db, test_user.id) is None
    for key in list(rows[0]):
        if key.endswith('_json'):
            del rows[0][key]
    accepted = await client.post('/data/import/commit', json={'csv': csv_text(rows)})
    assert accepted.status_code == 200, accepted.text
    assert await revision(db, test_user.id) == 1


def test_strong_repeated_optional_metadata_remains_compatible():
    text = 'Date,Workout Name,Duration,Workout Notes,Exercise Name,Set Order,Weight,Reps\n'
    text += '2026-08-20,Session,1h,Note,Pullup,1,0,8\n'
    text += '2026-08-20,Session,,,Pullup,2,0,8\n'
    parsed = parse_csv(text)
    assert parsed.workouts[0].total_sets == 2


@pytest.mark.parametrize('column', ['analytics_snapshot_json', 'analytics_load_defaults_json', 'analytics_load_snapshot_json', 'anomaly_review_json'])
def test_csv_context_schema_validation(column):
    rows = csv_rows()
    rows[0][column] = '{"schema_version": 99}'
    with pytest.raises(CsvParseError, match='Invalid'):
        parse_csv(csv_text(rows))


@pytest.mark.asyncio
async def test_foreign_journal_link_rejected_in_history_and_stripped_on_import(client, db, history_exercise):
    from datetime import date
    from uuid import uuid4
    from api.services.models import AppUser, WorkoutSession
    marker = uuid4().hex
    other = AppUser(firebase_uid=f'p108-foreign-{marker}', email=f'{marker}@example.com')
    db.add(other)
    await db.flush()
    foreign = UserAnthropometry(app_user_id=other.id, client_uuid='foreign-mass',
        measured_on=date(2026, 8, 20), weight=75, submitted_fields=['weight'])
    db.add(foreign)
    await db.commit()
    try:
        mass = deepcopy(MASS)
        mass['body_mass'].update(source='journal', measurement_id=foreign.id,
                                 measurement_client_uuid=foreign.client_uuid)
        body = history_body(history_exercise.id)
        body['analytics_snapshot'] = mass
        rejected = await client.post('/workouts/history', json=body)
        assert rejected.status_code == 409, rejected.text
        rows = csv_rows()
        rows[0]['analytics_snapshot_json'] = json.dumps(mass)
        rows[0]['Exercise Name'] = history_exercise.name
        accepted = await client.post('/data/import/commit', json={'csv': csv_text(rows)})
        assert accepted.status_code == 200, accepted.text
        saved = await db.scalar(select(WorkoutSession.analytics_snapshot).where(
            WorkoutSession.app_user_id == history_exercise.app_user_id))
        assert saved['body_mass']['measurement_id'] is None
        assert saved['body_mass']['source'] == 'imported'
        assert saved['body_mass']['import_provenance']['original_measurement_id'] == str(foreign.id)
        await db.refresh(foreign)
        assert foreign.weight == 75 and foreign.app_user_id == other.id
    finally:
        await db.delete(foreign)
        await db.delete(other)
        await db.commit()


@pytest.mark.asyncio
async def test_concurrent_import_is_idempotent(client, db, test_user, history_exercise):
    import asyncio
    rows = csv_rows()
    rows[0]['Exercise Name'] = history_exercise.name
    payload = {'csv': csv_text(rows)}
    results = await asyncio.gather(*(client.post('/data/import/commit', json=payload) for _ in range(2)))
    assert all(response.status_code == 200 for response in results)
    assert sorted(response.json()['added_workouts'] for response in results) == [0, 1]
    assert await revision(db, test_user.id) == 1


@pytest.mark.asyncio
async def test_import_without_duration_draft_preserves_unknown_end(client, db, test_user, history_exercise):
    from api.services.models import WorkoutSession
    rows = csv_rows()
    rows[0]['Exercise Name'] = history_exercise.name
    rows[0]['Duration'] = ''
    imported = await client.post('/data/import/commit', json={'csv': csv_text(rows)})
    assert imported.status_code == 200, imported.text
    wid = await db.scalar(select(WorkoutSession.id).where(WorkoutSession.app_user_id == test_user.id))
    read = await client.get(f'/workouts/history/{wid}/draft')
    assert read.status_code == 200, read.text
    draft = read.json()
    assert draft['finished_at'] is None
    draft['exercises'][0].pop('exercise_name')
    rejected = await client.patch(f'/workouts/{wid}/history', json=draft)
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_tombstoned_child_replay_without_actual_mutation_does_not_dirty(client, db, test_user, history_exercise):
    body = history_body(history_exercise.id)
    created = await client.post('/workouts/history', json=body)
    wid = created.json()['workout']['id']
    deletion = deepcopy(body)
    deletion['exercises'][0]['sets'] = []
    deleted = await client.patch(f'/workouts/{wid}/history', json=deletion)
    assert deleted.status_code == 200, deleted.text
    before = await revision(db, test_user.id)
    body['base_revision'] = deleted.json()['workout']['revision']
    replay = await client.patch(f'/workouts/{wid}/history', json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()['workout']['exercises'][0]['sets'] == []
    assert replay.json()['workout']['revision'] == body['base_revision']
    assert await revision(db, test_user.id) == before
