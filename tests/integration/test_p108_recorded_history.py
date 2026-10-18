"""Real pre-cutover rows: no synthetic resistance metadata or reconstructed history."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select, event

from api.services.models import WorkoutSession as W, WorkoutSessionExercise as E, WorkoutSessionSet as S
from api.services.analytics.rebuild import rebuild_user
from api.services.analytics.repository import load_facts
from api.services.analytics.types import AnalyticsQuery


@pytest.mark.asyncio
async def test_pre_cutover_scalar_history_populates_actual_exercise_and_muscle_routes(
        client, db, test_user, fresh_exercise, monkeypatch, tmp_path):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    fresh_exercise.main_muscle_group = 'chest'
    ids = []
    for offset, weight in enumerate((50, 55, 60, 65)):
        start = datetime.now(timezone.utc)-timedelta(days=8-offset)
        workout = W(app_user_id=test_user.id, source='free', status='finished',
                    started_at=start, finished_at=start+timedelta(hours=1))
        db.add(workout)
        await db.flush()
        exercise = E(workout_session_id=workout.id, exercise_id=fresh_exercise.id, order_index=0)
        db.add(exercise)
        await db.flush()
        row = S(workout_session_exercise_id=exercise.id, set_number=1, set_type='normal',
                weight=weight, reps=10, effort_level='medium', is_completed=True, is_anomalous=False)
        db.add(row)
        await db.flush()
        ids.append(row.id)
    await db.commit()
    await rebuild_user(db, test_user.id)
    writes = []
    def track(conn, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')):
            writes.append(statement)
    event.listen(db.bind.sync_engine, 'before_cursor_execute', track)
    try:
        response = await client.get(f'/progress/analytics/exercises/{fresh_exercise.id}?period=all')
        assert response.status_code == 200
        data = response.json()
        assert data['state'] == 'ready'
        assert len(data['points']) == 4
        assert [p['e1rm'] for p in data['points']] == pytest.approx([w*10**.1 for w in (50,55,60,65)])
        assert [p['volume'] for p in data['points']] == [500,550,600,650]
        assert data['points'][0]['details']['intensity']['value'] is None
        assert all(p['details']['intensity']['value'] is not None for p in data['points'][1:])
        assert data['summary']['strength']['delta_pct'] == pytest.approx((62.5/52.5-1)*100)
        assert data['coverage']['strength'] == {'used':4, 'total':4}
        assert data['summary']['volume']['tonnage']['value'] == 2300
        assert all(item['reason'] != 'recorded_load_basis' for item in data['excluded'])
        for point in data['points']:
            assert point['details']['load_context'] == dict(schema_version=1, model='recorded_load',
                input_basis='recorded', source='legacy_recorded_weight')
            assert 'recorded_load_basis' in point['details']['reasons']
        muscles = (await client.get('/progress/analytics/muscles?period=all')).json()
        assert any(m['delta_pct'] is not None for m in muscles['muscles'])
        assert not writes
    finally:
        event.remove(db.bind.sync_engine, 'before_cursor_execute', track)
    saved = (await db.execute(select(S.analytics_load_snapshot, S.load_snapshot, S.load_mode,
        E.analytics_load_defaults, W.analytics_snapshot).join(E, S.workout_session_exercise_id==E.id)
        .join(W, E.workout_session_id==W.id).where(S.id.in_(ids)))).all()
    assert all(all(value is None for value in row) for row in saved)
    (tmp_path/'exercise.json').write_text(json.dumps(data), encoding='utf-8')
    (tmp_path/'muscles.json').write_text(json.dumps(muscles), encoding='utf-8')
    print('Synthetic API DTO evidence:', tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('set_context,defaults', [
    ({'analytics_load_snapshot': {}}, None),
    ({'load_snapshot': {}}, None),
    ({'load_mode': 'plate_loaded'}, None),
    ({}, {}),
    ({}, {'schema_version':1,'model':'bodyweight','input_basis':'body_components'}),
    ({'analytics_load_snapshot': {'schema_version':1,'model':'bodyweight',
        'input_basis':'body_components','added_load_kg':0,'assistance_kg':0}}, None),
    ({'analytics_load_snapshot': {'schema_version':1,'model':'barbell','input_basis':'plates_only'}}, None),
    ({'analytics_load_snapshot': {'schema_version':1,'model':'dumbbell','input_basis':'per_implement'}}, None),
])
async def test_saved_incomplete_resistance_never_uses_naked_row_compatibility(
        db_session, app_user, exercise, set_context, defaults):
    start = datetime.now(timezone.utc)-timedelta(days=1)
    workout = W(app_user_id=app_user.id, source='free', status='finished',
        started_at=start, finished_at=start+timedelta(hours=1))
    db_session.add(workout)
    await db_session.flush()
    instance = E(workout_session_id=workout.id, exercise_id=exercise.id,
                 order_index=0, analytics_load_defaults=defaults)
    db_session.add(instance)
    await db_session.flush()
    db_session.add(S(workout_session_exercise_id=instance.id, set_number=1,
        set_type='normal', weight=50, reps=10, is_completed=True, **set_context))
    await db_session.flush()
    row, = (await load_facts(db_session, app_user.id, AnalyticsQuery(period='all'))).window_sets
    assert (row.load_context or {}).get('model') != 'recorded_load'
    assert row.tonnage_kg is None
    assert row.e1rm_weight_kg is None or row.e1rm_reps is None
