from __future__ import annotations
import uuid
import pytest

pytestmark = pytest.mark.asyncio


async def payload(client):
    exercise = await client.post('/exercises', json=dict(name=f'Execution {uuid.uuid4().hex[:8]}',
        main_muscle_group='Грудь', client_uuid=str(uuid.uuid4())))
    assert exercise.status_code in (200, 201), exercise.text
    exercise_id = exercise.json()['id']
    set_uuid = str(uuid.uuid4())
    return dict(client_uuid=str(uuid.uuid4()), source='free', status='active',
        started_at='2026-10-18T12:00:00Z', execution_state=dict(schema_version=1, enabled=True,
        preference_epoch='legacy-default-v1', initialized_at='2026-10-18T12:00:00Z',
        changed_at='2026-10-18T12:00:00Z', banner_dismissed_at=None), exercises=[dict(
        client_uuid=str(uuid.uuid4()), exercise_id=exercise_id, order_index=0, target_sets=3,
        execution_state=dict(schema_version=1, exercise_id=exercise_id, slots={
            '1':dict(state='draft', set_client_uuid=set_uuid, updated_at='2026-10-18T12:00:00Z'),
            '2':dict(state='forecast', set_client_uuid=None, updated_at='2026-10-18T12:00:00Z')}),
        sets=[dict(client_uuid=set_uuid, set_number=1, weight=60, reps=None, is_completed=False)])])


async def test_execution_state_round_trip_and_old_client_omission(client):
    body = await payload(client)
    first = await client.post('/sync/workouts', json=body)
    assert first.status_code == 200, first.text
    response = first.json()
    assert response['workout']['execution_state']['enabled'] is True
    exercise = response['workout']['exercises'][0]
    assert exercise['execution_state']['slots']['1']['state'] == 'draft'
    assert len(exercise['sets']) == 1
    assert exercise['sets'][0]['is_completed'] is False
    assert exercise['sets'][0]['reps'] is None
    body.pop('execution_state')
    body['exercises'][0].pop('execution_state')
    body['base_version'] = response['sync_version']
    second = await client.post('/sync/workouts', json=body)
    assert second.status_code == 200, second.text
    assert second.json()['workout']['execution_state']['enabled'] is True
    assert second.json()['workout']['exercises'][0]['execution_state']['slots']['1']['state'] == 'draft'


async def test_execution_slot_rejects_foreign_or_missing_uuid(client):
    body = await payload(client)
    body['exercises'][0]['execution_state']['slots']['1']['set_client_uuid'] = str(uuid.uuid4())
    response = await client.post('/sync/workouts', json=body)
    assert response.status_code == 422, response.text

@pytest.mark.parametrize('already_synced',[False,True])
async def test_deleting_execution_draft_retires_binding_for_sync_and_older_clients(client,already_synced):
    body=await payload(client)
    if already_synced:
        initial=await client.post('/sync/workouts',json=body)
        assert initial.status_code==200,initial.text
        body['base_version']=initial.json()['sync_version']
        # An older client deletes a real row without understanding execution metadata.
        body['exercises'][0].pop('execution_state')
    body['exercises'][0]['sets'][0]['deleted']=True
    result=await client.post('/sync/workouts',json=body)
    assert result.status_code==200,result.text
    ex=result.json()['workout']['exercises'][0]
    assert ex['sets']==[]
    assert ex['execution_state']['slots']['1']['state']=='forecast'
    assert ex['execution_state']['slots']['1']['set_client_uuid'] is None
    body['base_version']=result.json()['sync_version']
    body['exercises'][0]['execution_state']=ex['execution_state']
    replay=await client.post('/sync/workouts',json=body)
    assert replay.status_code==200,replay.text

async def test_legacy_delete_endpoint_retires_execution_binding(client):
    body=await payload(client)
    initial=await client.post('/sync/workouts',json=body)
    detail=initial.json()['workout']; row=detail['exercises'][0]['sets'][0]
    result=await client.delete(f"/workout-session-sets/{row['id']}")
    assert result.status_code==204,result.text
    detail=(await client.get(f"/workouts/{detail['id']}")).json()
    assert detail['exercises'][0]['execution_state']['slots']['1']['state']=='forecast'
