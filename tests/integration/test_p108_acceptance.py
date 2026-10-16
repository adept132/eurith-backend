"""Cross-layer acceptance: real mobile wire/ACK and HTTP history mutations.

P108_MOBILE_ROOT selects a sibling mobile checkout for the optional TCP/JS seam.
Run with scripts/run_p108_tests.py against the guarded local fixture database.
"""
import asyncio
import json
import os
import socket
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from api.main import app
from api.services.models import UserAnthropometry, TrainingBlock, PeriodReport
from api.services.analytics.rebuild import rebuild_user
from api.services.reports import service as reports
from tests.integration.test_workout_history_api import history_body

pytestmark = pytest.mark.asyncio
BASE = '/progress/analytics'
LOAD = {'schema_version': 1, 'model': 'barbell', 'input_basis': 'full'}


async def test_legacy_client_cutover_keeps_formula_channels_separate(client, db, test_user, fresh_exercise, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    for offset, weight in enumerate((100, 110)):
        body = history_body(fresh_exercise.id, weight=str(weight))
        start = datetime.now(timezone.utc)-timedelta(days=4-offset)
        body.update(client_uuid=str(uuid4()), started_at=start.isoformat(), finished_at=(start+timedelta(hours=1)).isoformat())
        body['exercises'][0].update(client_uuid=str(uuid4()), analytics_load_defaults=LOAD)
        body['exercises'][0]['sets'][0].update(client_uuid=str(uuid4()), analytics_load_snapshot=LOAD, reps=8)
        assert (await client.post('/workouts/history', json=body)).status_code == 201
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'false')
    legacy = (await client.get('/progress/achievements')).json()
    assert legacy[0]['e1rm'] == round(110*36/29, 1)
    assert (await client.get(f'{BASE}/capabilities')).json()['state'] == 'disabled'
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    await rebuild_user(db, test_user.id)
    new = (await client.get(f'{BASE}/exercises/{fresh_exercise.id}?period=all')).json()
    assert new['formula_id'] == 'lombardi-v1' and new['rules_version'] == 'p1-08-v1'
    assert new['record_baseline']['best_e1rm'] == pytest.approx(110*8**.1)
    assert new['record_events'][0]['previous_e1rm'] == pytest.approx(100*8**.1)
    assert (await client.get('/progress/achievements')).json() == legacy
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'false')
    assert (await client.get('/progress/achievements')).json() == legacy
    assert (await client.get(f'{BASE}/exercises/{fresh_exercise.id}?period=all')).json()['state'] == 'disabled'


async def test_server_only_history_anomaly_recovers_through_full_history_editor(client, db, test_user, fresh_exercise, monkeypatch):
    """Client payload contains no history; server owns the three prior sessions.

    HTTP fixture exercises the same full-draft confirmation/revocation contract
    used by the mobile history editor; it does not claim native UI execution.
    """
    from tests.integration.test_p108_write_paths import snapshot
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    for offset in range(3):
        body = history_body(fresh_exercise.id, weight='100')
        start = datetime.now(timezone.utc)-timedelta(days=6-offset)
        body.update(client_uuid=str(uuid4()), started_at=start.isoformat(), finished_at=(start+timedelta(hours=1)).isoformat())
        body['exercises'][0].update(client_uuid=str(uuid4()), analytics_load_defaults=LOAD)
        body['exercises'][0]['sets'][0].update(client_uuid=str(uuid4()), analytics_load_snapshot=LOAD, reps=8)
        response = await client.post('/workouts/history', json=body)
        assert response.status_code == 201, response.text
    wire = snapshot(fresh_exercise.id)
    wire['exercises'][0]['analytics_load_defaults'] = LOAD
    wire['exercises'][0]['sets'][0].update(weight=300, reps=8, is_completed=True)
    saved = await client.post('/sync/workouts', json=wire)
    assert saved.status_code == 200, saved.text
    workout = saved.json()['workout']; row = workout['exercises'][0]['sets'][0]
    assert row['is_anomalous'] and 'e1rm_jump' in row['anomaly_review']['reason_codes']
    assert (await client.post(f"/workouts/{workout['id']}/finish")).status_code == 200
    values = []
    for confirmed in (True, False):
        draft = (await client.get(f"/workouts/history/{workout['id']}/draft")).json()
        draft['exercises'][0].pop('exercise_name', None)
        draft['exercises'][0]['sets'][0]['anomaly_review']['confirmed'] = confirmed
        changed = await client.patch(f"/workouts/{workout['id']}/history", json=draft)
        assert changed.status_code == 200, changed.text
        assert changed.json()['workout']['exercises'][0]['sets'][0]['is_anomalous'] is not confirmed
        await rebuild_user(db, test_user.id)
        analytics = (await client.get(f'{BASE}/exercises/{fresh_exercise.id}?period=all')).json()
        values.append(analytics['summary']['best_e1rm']['value'])
    assert values == pytest.approx([300*8**.1, 100*8**.1])


async def test_mobile_serializer_canonical_ack_restart_replay(client, db, test_user, fresh_exercise, monkeypatch, tmp_path):
    import uvicorn
    mobile = Path(os.environ.get('P108_MOBILE_ROOT', '../p1-08-mobile')).resolve()
    assert (mobile/'src/offline/__tests__/analyticsHttpAcceptance.test.ts').is_file(), 'Set P108_MOBILE_ROOT to the matching mobile checkout'
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    sock = socket.socket(); sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='off'))
    server.install_signal_handlers = lambda: None
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    exchange = tmp_path/'exchange.json'
    try:
        for _ in range(100):
            if server.started: break
            await asyncio.sleep(.05)
        assert server.started
        env = dict(os.environ, P108_QA_HTTP=f'http://127.0.0.1:{port}', P108_QA_UID=test_user.firebase_uid,
                   P108_QA_EXERCISE=str(fresh_exercise.id), P108_QA_EXCHANGE=str(exchange))
        evidence = Path('docs/validation/p1-08/artifacts/task21'); evidence.mkdir(parents=True, exist_ok=True)
        with (evidence/'mobile-http-wire.log').open('wb') as log:
            process = await asyncio.create_subprocess_exec('node', 'node_modules/jest/bin/jest.js', '--runInBand',
                '--runTestsByPath', 'src/offline/__tests__/analyticsHttpAcceptance.test.ts', '--watchman=false', cwd=mobile,
                env=env, stdout=log, stderr=asyncio.subprocess.STDOUT)
            try: await asyncio.wait_for(process.wait(), 100)
            except TimeoutError:
                process.kill();await process.wait();raise
        output=(evidence/'mobile-http-wire.log').read_bytes()
        assert process.returncode == 0, output.decode(errors='replace')
        result = json.loads(exchange.read_text())
        (evidence/'mobile-http-exchange.json').write_text(json.dumps(result, indent=2))
        assert await db.scalar(select(func.count()).select_from(UserAnthropometry).where(
            UserAnthropometry.app_user_id == test_user.id, UserAnthropometry.client_uuid == result['measurement'])) == 1
        wid = result['response']['workout']['id']
        finished = await client.post(f'/workouts/{wid}/finish')
        assert finished.status_code == 200, finished.text
        ready = await rebuild_user(db, test_user.id)
        data = (await client.get(f'{BASE}/exercises/{fresh_exercise.id}?period=all')).json()
        assert data['data_revision'] == ready['source_revision']
        assert data['summary']['best_e1rm']['value'] == pytest.approx(85 * 8**.1)
    finally:
        server.should_exit = True
        await serving
        sock.close()


async def test_end_to_end_fact_edit_and_replay(client, db, test_user, fresh_exercise, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    monkeypatch.setattr(reports, 'PERIOD_TYPES', ('week',))
    blocks = [TrainingBlock(app_user_id=test_user.id, block_index=i, microcycle_length=7,
        start_date=date(2026,8,3+i*7), planned_end_date=date(2026,8,9+i*7),
        actual_end_date=date(2026,8,9+i*7), status='closed') for i in range(2)]
    old = PeriodReport(app_user_id=test_user.id,period_type='week',period_start=date(2026,7,27),
        period_end=date(2026,8,2),rules_version=2,shape_version=2,payload={'metrics':{'preserved':'legacy'},'actions':[]})
    db.add_all([*blocks, old]); await db.commit(); original = deepcopy(old.payload)
    workouts=[]
    for offset,weight in enumerate([100,105,110,115,120,125,130,135]):
        body=history_body(fresh_exercise.id, weight=str(weight));stamp=datetime(2026,8,4,tzinfo=timezone.utc)+timedelta(days=offset)
        body.update(client_uuid=str(uuid4()),started_at=stamp.isoformat(),finished_at=(stamp+timedelta(hours=1)).isoformat(),analytics_snapshot={'schema_version':1,'is_deload':False})
        body['exercises'][0].update(client_uuid=str(uuid4()),analytics_load_defaults=LOAD)
        body['exercises'][0]['sets'][0].update(client_uuid=str(uuid4()),analytics_load_snapshot=LOAD,reps=1)
        response=await client.post('/workouts/history',json=body)
        assert response.status_code==201,response.text
        workouts.append(response.json()['workout']['id'])
    async def read_all():
        ready=await rebuild_user(db,test_user.id)
        paths=[f'/exercises/{fresh_exercise.id}?period=all','/muscles?period=all',
               f'/blocks/compare?block_a_id={blocks[0].id}&block_b_id={blocks[1].id}']
        data=[]
        for path in paths:
            response=await client.get(BASE+path);assert response.status_code==200,response.text
            item=response.json();assert item['state']=='ready';assert item['data_revision']==ready['source_revision'];assert item['rules_version']=='p1-08-v1'
            data.append(item)
        await db.refresh(old);assert old.payload==original
        return data
    before=await read_all()
    assert before[0]['summary']['best_e1rm']['value']==135
    draft=(await client.get(f'/workouts/history/{workouts[0]}/draft')).json()
    for ex in draft['exercises']:ex.pop('exercise_name',None)
    draft['exercises'][0]['sets'][0]['weight']='140'
    changed=await client.patch(f'/workouts/{workouts[0]}/history',json=draft);assert changed.status_code==200,changed.text
    replay=await client.patch(f'/workouts/{workouts[0]}/history',json=draft);assert replay.status_code==200,replay.text
    edited=await read_all()
    assert edited[0]['summary']['best_e1rm']['value']==140
    assert edited[0]['summary']['intensity']!=before[0]['summary']['intensity']
    assert edited[1]['volume']['tonnage']!=before[1]['volume']['tonnage']
    assert edited[2]['a']['volume']['tonnage']!=before[2]['a']['volume']['tonnage']
    deleted=await client.delete(f'/workouts/{workouts[0]}');assert deleted.status_code==204,deleted.text
    after=await read_all()
    assert after[0]['summary']['best_e1rm']['value']==135
    assert after[0]['summary']['intensity']!=edited[0]['summary']['intensity']
    assert len(after[0]['points'])==7
    await reports.ensure_reports(db,test_user.id,date(2026,8,17));await db.commit()
    new=(await client.get('/reports/week/2026-08-10')).json()
    assert new['analytics_meta']['data_revision']==after[0]['data_revision']
    assert new['analytics_meta']['formula_id']=='lombardi-v1'
    await db.refresh(old);assert old.payload==original
