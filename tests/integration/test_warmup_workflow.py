"""Real HTTP → persisted workout → history/progression integration contracts."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from types import SimpleNamespace

import pytest
from api.services.progression import repository
from api.services.load_context import EffectiveLoadContext
from api.services.models import Exercise
from api.services.progression.state import evaluate
from api.services.progression.schemes.fixed_increment import _last_session_completed
from tests.integration.test_sync_reliability import _exercise_id, _snapshot
from tests.integration.test_sync_warmup_snapshots import group_fixture, row_fixture, push

pytestmark = pytest.mark.asyncio


def prescription(numbers=(1, 2, 3)):
    return {"scheme": "fixed_increment", "sets": [
        {"set_number": n, "weight_kg": 80, "rep_min": reps, "rep_max": reps, "rir": 2, "kind": "normal"}
        for n, reps in zip(numbers, (10, 8, 5))], "reason_code": "test", "reason_text": "",
        "basis": {}, "engine_version": 2, "provisional": False}


@pytest.mark.parametrize("encoding", ["canonical", "client_live", "client_shown", "client_partial"])
@pytest.mark.parametrize("numbers", [(4, 5, 6), (4, 6, 7)])
@pytest.mark.parametrize("filtered", [False, True])
async def test_client_numbering_to_history_keeps_heterogeneous_working_goals(client, db, test_user, encoding, numbers, filtered):
    """Nine reps misses the first 10-rep goal; technical warmup slots must not select final 5."""
    payload = _snapshot(str(uuid4()), await _exercise_id(client))
    payload.update(status="finished", finished_at=datetime.now(timezone.utc).isoformat())
    ex = payload["exercises"][0]
    catalog = await db.get(Exercise, ex['exercise_id'])
    catalog.equipment_needed = ['block_machine', 'free_machine']
    await db.commit()
    ex["prescription"] = prescription()
    # This is the real pre-P1-10 ExerciseWorkspace persisted live/shown-target shape:
    # client loop ordinals are remapped to factual technical slots at serialization.
    if encoding != "canonical":
        ex["live_prescription"] = prescription(numbers)
    ex["sets"] = [{"client_uuid": str(uuid4()), "set_number": n, "set_type": "warmup",
        "weight": 20, "reps": 16, "is_completed": True} for n in (1, 2, 3)]
    for ordinal, (number, reps) in enumerate(zip(numbers, (10, 7, 5) if filtered else (9, 8, 5))):
        row = {"client_uuid": str(uuid4()), "set_number": number, "set_type": "normal",
            "weight": 80, "reps": reps, "effort_level": "medium", "is_completed": True}
        row['load_mode'] = 'plate_loaded' if filtered and ordinal == 0 else 'stack'
        if encoding == "client_shown" or (encoding == "client_partial" and ordinal == 0):
            row["shown_target_snapshot"] = prescription(numbers)["sets"][ordinal]
        ex["sets"].append(row)
    saved = await push(client, payload)
    context = EffectiveLoadContext(None, None, None, 'stack', 2.5, 'kg', 1, 'displayed', None, None) if filtered else None
    history = await repository.load_history(db, test_user.id, ex["exercise_id"], load_context=context)
    session = next(s for s in history.sessions if s.session_id == saved["id"])
    assert [(s.set_number, s.working_ordinal) for s in session.sets if s.set_type == "normal"] == list(zip(numbers, (1, 2, 3)))[1 if filtered else 0:]
    assert _last_session_completed(SimpleNamespace(history=history)) is False
    assert evaluate(session.prescription, session.sets, 2.5, initial_prescription=session.initial_prescription).status == "miss"
    detail = (await client.get(f"/workouts/{saved['id']}")).json()["exercises"][0]
    assert [r["set_number"] for r in detail["sets"] if r["set_type"] == "normal"] == list(numbers)
    if encoding == "client_shown":
        assert [r["shown_target_snapshot"]["set_number"] for r in detail["sets"] if r["set_type"] == "normal"] == list(numbers)

@pytest.mark.parametrize('effort_alias', ['warmup', 'warmup_effort'])
async def test_server_roundtrip_old_client_and_history(client, db, test_user, effort_alias):
    payload = _snapshot(str(uuid4()), await _exercise_id(client))
    ex = payload["exercises"][0]
    group = group_fixture()
    version = group["versions"][0]
    version.update(source_e1rm_kg=112)
    version["source"].update(reps=10)
    version["settings"].update(count=3, between_seconds=30, category="compound", count_origin="global")
    context = version["equipment"]
    context.update(kind="barbell", loading_sides=2, weight_basis="plates_only", step=None, bar={"weight":15,"unit":"kg"})
    version["source"]["load_context"] = deepcopy(context)
    ex["warmup_group"] = group
    raw, reps, rounded = (17.5, 112/3, 60), (16,13,10), (17.5,35,60)
    ex["sets"] = []
    for i in range(3):
        metadata = row_fixture(group,i+1)
        metadata.update(raw_weight_kg=raw[i])
        ex["sets"].append({"client_uuid":str(uuid4()),"set_number":i+1,"set_type":"warmup",
            "weight":rounded[i],"reps":reps[i],"is_completed":True,"effort_level":effort_alias,
            "warmup_snapshot":metadata,"load_snapshot":{"equipment":deepcopy(context)}})
    for i in range(3):
        ex["sets"].append({"client_uuid":version["source"]["set_client_uuid"] if i==0 else str(uuid4()),
            "set_number":i+4,"set_type":"normal","weight":80,"reps":10,"is_completed":True,"effort_level":"medium"})
    async def push_versioned(value):
        response = await client.post('/sync/workouts', json=value)
        assert response.status_code == 200, response.text
        body = response.json()
        return body['workout'], body['sync_version']

    original, version_number = await push_versioned(payload)
    assert [r['effort_level'] for r in original['exercises'][0]['sets'][:3]] == [effort_alias]*3
    retry, version_number = await push_versioned(payload)
    assert [r["id"] for r in retry["exercises"][0]["sets"]] == [r["id"] for r in original["exercises"][0]["sets"]]
    assert len({r["client_uuid"] for r in retry["exercises"][0]["sets"]}) == 6
    # Actual old-client omission must not erase group/row/load context.
    legacy=deepcopy(payload)
    legacy["base_version"]=version_number
    del legacy["exercises"][0]["warmup_group"]
    for row in legacy["exercises"][0]["sets"]:
        row.pop("warmup_snapshot",None);row.pop("load_snapshot",None)
    legacy["notes"]="old client"
    kept, version_number = await push_versioned(legacy)
    assert kept["exercises"][0]["warmup_group"] == group
    assert kept["exercises"][0]["sets"][0]["warmup_snapshot"] == ex["sets"][0]["warmup_snapshot"]
    assert kept["exercises"][0]["sets"][0]["load_snapshot"] == ex["sets"][0]["load_snapshot"]
    stale=await client.post('/sync/workouts',json={**legacy,"notes":"stale replay"})
    assert stale.status_code==409,stale.text
    legacy["base_version"]=version_number
    recovered=await push(client,legacy)
    assert len(recovered["exercises"][0]["sets"])==6
    finished=await client.post(f"/workouts/{original['id']}/finish")
    assert finished.status_code==200,finished.text
    assert (finished.json()["sets_count"],finished.json()["total_reps"],float(finished.json()["total_volume"])) == (3,30,2400)
    detail=(await client.get(f"/workouts/{original['id']}")).json()
    assert detail["status"]=="finished"
    assert len([r for r in detail["exercises"][0]["sets"] if r["set_type"]=="warmup"])==3
    assert detail["exercises"][0]["warmup_group"]==group
    history=await repository.load_history(db,test_user.id,ex["exercise_id"])
    assert sum((r.weight_kg or 0)*r.reps for r in history.sessions[0].sets if r.set_type=='normal')==2400




@pytest.mark.parametrize('mode', [None, 'stack', 'plate_loaded'])
async def test_captured_equipment_envelope_preserves_authoritative_core(client, db, mode):
    payload=_snapshot(str(uuid4()), await _exercise_id(client))
    ex=payload['exercises'][0]
    catalog=await db.get(Exercise,ex['exercise_id'])
    catalog.equipment_needed=['barbell'] if mode is None else ['block_machine','free_machine']
    await db.commit()
    row=ex['sets'][0]
    row.update(load_mode=mode,gym_profile_id=None,setup_id=None)
    initial=await push(client,payload)
    core=deepcopy(initial['exercises'][0]['sets'][0]['load_snapshot'])
    equipment=deepcopy(group_fixture()['versions'][0]['equipment'])
    equipment.update(kind='barbell' if mode is None else 'step' if mode=='stack' else 'plate_loaded',
        load_mode=mode, loading_sides=2 if mode is None else core['loading_sides'],
        weight_basis='plates_only' if mode is None else core['weight_basis'],
        base_weight_kg=None if mode is None else core['base_weight'],
        bar={'weight':15,'unit':'kg'} if mode is None else None,
        step=None if mode is None else {'value':core['step_value'],'unit':core['step_unit']})
    envelope={**(core or {}),'display_unit':'kg','equipment':equipment}
    row.update(client_uuid=str(uuid4()),set_number=2,load_snapshot=envelope)
    accepted=await push(client,payload)
    saved=next(r for r in accepted['exercises'][0]['sets'] if r['client_uuid']==row['client_uuid'])
    assert saved['load_snapshot']==envelope
    # New identities make these fresh validation attempts, not historical replay exceptions.
    for field,value in [('display_unit','stone'),('unknown',True),('equipment',None)]:
        bad=deepcopy(payload);target=bad['exercises'][0]['sets'][0]
        target['client_uuid']=str(uuid4());target['load_snapshot'][field]=value
        assert (await client.post('/sync/workouts',json=bad)).status_code in (400,422)
    bad=deepcopy(payload);target=bad['exercises'][0]['sets'][0]
    target['client_uuid']=str(uuid4());target['load_snapshot']['equipment']['gym_profile_id']=str(uuid4())
    assert (await client.post('/sync/workouts',json=bad)).status_code in (400,422)
    if mode is not None:
        for field,value in [('step_value',999),('plates',[{'weight':25,'unit':'kg','count':999}])]:
            bad=deepcopy(payload);target=bad['exercises'][0]['sets'][0]
            target['client_uuid']=str(uuid4());target['load_snapshot'][field]=value
            assert (await client.post('/sync/workouts',json=bad)).status_code in (400,422)



@pytest.mark.parametrize('alias', ['warmup','warmup_effort'])
async def test_effort_alias_never_changes_explicit_working_role(client, alias):
    payload=_snapshot(str(uuid4()),await _exercise_id(client))
    row=payload['exercises'][0]['sets'][0]
    row.update(set_type='normal',effort_level=alias,weight=80,reps=10,is_completed=True)
    saved=await push(client,payload)
    assert saved['exercises'][0]['sets'][0]['set_type']=='normal'
    assert saved['exercises'][0]['sets'][0]['effort_level']==alias
    detail=(await client.get(f"/workouts/{saved['id']}")).json()
    assert detail['exercises'][0]['sets'][0]['effort_level']==alias
    response=await client.post(f"/workouts/{saved['id']}/finish")
    assert response.status_code==200,response.text
    assert response.json()['sets_count']==1
    assert float(response.json()['total_volume'])==800

async def test_sparse_legacy_fact_lookup_and_positional_constructor_stay_compatible():
    from api.services.progression.types import SetFact, SetPrescription, Prescription, SessionFact, ExerciseHistory
    target=SetPrescription(9,80,10,10,2,'normal')
    p=Prescription('fixed_increment',(target,),reason_code='test',reason_text='')
    fact=SetFact(9,80,9,2,'normal',False,None,None)
    assert fact.prescription_set_number is None
    assert evaluate(p,(fact,),2.5).status=='miss'
    history=ExerciseHistory(1,(SessionFact(1,datetime.now(timezone.utc),p,(fact,)),))
    assert _last_session_completed(SimpleNamespace(history=history)) is False

async def test_deleted_completed_generated_warmup_cannot_return_on_snapshot_replay(client):
    payload = _snapshot(str(uuid4()), await _exercise_id(client))
    exercise = payload['exercises'][0]
    group = group_fixture()
    exercise['warmup_group'] = group
    row = exercise['sets'][0]
    row.update(set_type='warmup', effort_level='warmup', is_completed=True,
               warmup_snapshot=row_fixture(group, 1))
    saved = await push(client, payload)
    deleted = saved['exercises'][0]['sets'][0]
    response = await client.delete(f"/workout-session-sets/{deleted['id']}")
    assert response.status_code == 204, response.text
    result = await push(client, payload)
    assert deleted['client_uuid'] not in {r['client_uuid'] for r in result['exercises'][0]['sets']}
    assert any(t['client_uuid'] == deleted['client_uuid'] for t in result['child_tombstones']['sets'])
