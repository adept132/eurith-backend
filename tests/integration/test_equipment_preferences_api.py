"""Real PostgreSQL coverage for owner/gym/exercise/context equipment choices."""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from api.services.models import AppUser, Exercise


@pytest_asyncio.fixture
async def equipment_exercises(db, test_user):
    other = AppUser(firebase_uid=f"equipment-{uuid.uuid4()}", email=f"equipment-{uuid.uuid4()}@example.com")
    db.add(other)
    await db.flush()
    rows = [Exercise(name=f"equipment-{uuid.uuid4()}", category="base", main_muscle_group="chest",
                     difficulty="beginner", source="test", app_user_id=owner)
            for owner in (None, test_user.id, other.id)]
    db.add_all(rows)
    await db.commit()
    yield rows
    await db.execute(delete(Exercise).where(Exercise.id.in_([row.id for row in rows])))
    await db.execute(delete(AppUser).where(AppUser.id == other.id))
    await db.commit()


def gym_payload(name="Equipment gym", revision=0, *, collars=None, bars=None):
    return {"name": name, "equipment": ["barbell"],
            "bars": bars if bars is not None else [{"weight": 15, "unit": "kg", "count": 1}],
            "discs": [], "steps": [], "collars": collars or [], "expected_revision": revision}


def preference(context="barbell", *, row_id=None, bar=None, collar=None, revision=0):
    return {"id": row_id or str(uuid.uuid4()), "context_key": context, "selected_bar": bar,
            "selected_collar_id": collar, "expected_revision": revision}


@pytest.mark.asyncio
async def test_equipment_selection_is_context_scoped(client, auth_headers, equipment_exercises, db):
    gym_id = str(uuid.uuid4())
    collar = {"id": str(uuid.uuid4()), "weight": 0.25, "unit": "kg", "count": 2}
    assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers,
                            json=gym_payload(collars=[collar]))).status_code == 200
    path = f"/gym-profiles/{gym_id}/equipment-preferences/global/{equipment_exercises[0].id}"
    first = await client.put(path, headers=auth_headers,
                            json=preference(bar={"weight": 15, "unit": "kg"}, collar=collar["id"]))
    assert first.status_code == 200, first.text
    first_context = first.json()
    assert first_context["selected_bar"] == {"weight": 15, "unit": "kg"}
    second = await client.put(path, headers=auth_headers, json=preference("machine:other"))
    assert second.status_code == 200, second.text
    other_context = second.json()
    assert other_context["selected_collar_id"] is None
    rows = (await client.get(f"/gym-profiles/{gym_id}/equipment-preferences", headers=auth_headers)).json()
    assert len(rows) == 2
    assert {r["context_key"] for r in rows} == {"barbell", "machine:other"}
    assert next(r for r in rows if r["context_key"] == "barbell")["selected_collar_id"] == collar["id"]

    from api.schemas.gym_profiles import GymProfilePayload
    from api.services.gym_profiles import put_gym_profile
    foreign_gym = uuid.uuid4()
    await put_gym_profile(db, equipment_exercises[2].app_user_id, foreign_gym,
                          GymProfilePayload.model_validate(gym_payload()))
    foreign_gym_response = await client.get(f"/gym-profiles/{foreign_gym}/equipment-preferences", headers=auth_headers)
    assert foreign_gym_response.status_code in (403, 404)
    foreign_put = await client.put(f"/gym-profiles/{foreign_gym}/equipment-preferences/global/{equipment_exercises[0].id}",
                                  headers=auth_headers, json=preference())
    assert foreign_put.status_code == 404


@pytest.mark.asyncio
async def test_equipment_preference_retry_revision_and_uuid_scope(client, auth_headers, equipment_exercises):
    gym_id = str(uuid.uuid4())
    assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers, json=gym_payload())).status_code == 200
    path = f"/gym-profiles/{gym_id}/equipment-preferences/global/{equipment_exercises[0].id}"
    payload = preference(bar={"weight": 15, "unit": "kg"})
    first = await client.put(path, headers=auth_headers, json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1
    assert (await client.put(path, headers=auth_headers, json=payload)).json() == first.json()
    changed_payload = {**payload, "selected_bar": None, "expected_revision": 1}
    changed = await client.put(path, headers=auth_headers, json=changed_payload)
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    assert (await client.put(path, headers=auth_headers, json=changed_payload)).json() == changed.json()
    conflict = await client.put(path, headers=auth_headers, json=payload)
    assert conflict.status_code == 409 and conflict.json()["detail"]["current"] == changed.json()
    duplicate = await client.put(path, headers=auth_headers, json=preference())
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["current"] == changed.json()
    reused = await client.put(path, headers=auth_headers, json={**payload, "context_key": "other"})
    assert reused.status_code == 404
    missing = await client.put(path, headers=auth_headers, json=preference("other", revision=1))
    assert missing.status_code == 404
    rows = (await client.get(f"/gym-profiles/{gym_id}/equipment-preferences", headers=auth_headers)).json()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_equipment_preference_source_exercise_and_gym_isolation(client, auth_headers, equipment_exercises):
    global_ex, owned_ex, foreign_ex = equipment_exercises
    gym_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for i, gym_id in enumerate(gym_ids):
        assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers,
                                json=gym_payload(f"Gym {i}"))).status_code == 200
    for source, ex in (("global", owned_ex), ("user", global_ex), ("user", foreign_ex)):
        denied = await client.put(f"/gym-profiles/{gym_ids[0]}/equipment-preferences/{source}/{ex.id}",
                                  headers=auth_headers, json=preference())
        assert denied.status_code == 404
    for source, ex in (("global", global_ex), ("user", owned_ex)):
        response = await client.put(f"/gym-profiles/{gym_ids[0]}/equipment-preferences/{source}/{ex.id}",
                                   headers=auth_headers, json=preference())
        assert response.status_code == 200, response.text
    second_gym = await client.get(f"/gym-profiles/{gym_ids[1]}/equipment-preferences", headers=auth_headers)
    assert second_gym.json() == []
    first_rows = (await client.get(f"/gym-profiles/{gym_ids[0]}/equipment-preferences", headers=auth_headers)).json()
    assert {(r["exercise_source"], r["exercise_id"]) for r in first_rows} == {("global", global_ex.id), ("user", owned_ex.id)}
    reuse = await client.put(f"/gym-profiles/{gym_ids[1]}/equipment-preferences/global/{global_ex.id}",
                            headers=auth_headers, json=preference(row_id=first_rows[0]["id"]))
    assert reuse.status_code == 404
    assert (await client.request("DELETE", f"/gym-profiles/{gym_ids[0]}", headers=auth_headers,
                                 json={"expected_revision": 1})).status_code == 200
    assert (await client.get(f"/gym-profiles/{gym_ids[0]}/equipment-preferences", headers=auth_headers)).status_code == 404
    assert (await client.put(f"/gym-profiles/{gym_ids[0]}/equipment-preferences/global/{global_ex.id}",
                             headers=auth_headers, json=preference())).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("bar,collar", [({"weight": 20, "unit": "kg"}, None),
                                      ({"weight": 15, "unit": "lb"}, None),
                                      (None, "missing"), (None, "zero"),
                                      ({"weight": 10, "unit": "kg"}, None)])
async def test_equipment_preference_rejects_unavailable_inventory(client, auth_headers, equipment_exercises, bar, collar):
    gym_id = str(uuid.uuid4())
    zero_collar = {"id": str(uuid.uuid4()), "weight": 0.25, "unit": "kg", "count": 0}
    payload = gym_payload(collars=[zero_collar], bars=[{"weight": 15, "unit": "kg", "count": 1},
                                                    {"weight": 10, "unit": "kg", "count": 0}])
    assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers, json=payload)).status_code == 200
    collar_id = zero_collar["id"] if collar == "zero" else str(uuid.uuid4()) if collar else None
    invalid = await client.put(f"/gym-profiles/{gym_id}/equipment-preferences/global/{equipment_exercises[0].id}",
                               headers=auth_headers, json=preference(bar=bar, collar=collar_id))
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["detail"]["code"] == "equipment_selection_requires_reselection"
    assert (await client.get(f"/gym-profiles/{gym_id}/equipment-preferences", headers=auth_headers)).json() == []


@pytest.mark.asyncio
async def test_inventory_removal_keeps_saved_selection_and_requires_reselection(client, auth_headers, equipment_exercises):
    gym_id = str(uuid.uuid4())
    collar = {"id": str(uuid.uuid4()), "weight": 0.25, "unit": "kg", "count": 1}
    assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers,
                            json=gym_payload(collars=[collar]))).status_code == 200
    path = f"/gym-profiles/{gym_id}/equipment-preferences/global/{equipment_exercises[0].id}"
    payload = preference(bar={"weight": 15, "unit": "kg"}, collar=collar["id"])
    first = await client.put(path, headers=auth_headers, json=payload)
    assert first.status_code == 200, first.text
    assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers,
                            json=gym_payload(revision=1, collars=[], bars=[]))).status_code == 200
    listed = (await client.get(f"/gym-profiles/{gym_id}/equipment-preferences", headers=auth_headers)).json()
    assert listed == [first.json()]
    stale_retry = await client.put(path, headers=auth_headers, json=payload)
    assert stale_retry.status_code == 422
    assert stale_retry.json()["detail"]["code"] == "equipment_selection_requires_reselection"
    cleared = await client.put(path, headers=auth_headers,
                              json=preference(row_id=payload["id"], revision=1))
    assert cleared.status_code == 200 and cleared.json()["revision"] == 2
    assert cleared.json()["selected_bar"] is None and cleared.json()["selected_collar_id"] is None


@pytest.mark.asyncio
async def test_equipment_preference_foreign_owner_uuid_cannot_be_reused(client, auth_headers, equipment_exercises, db):
    from api.schemas.gym_profiles import EquipmentPreferencePayload, GymProfilePayload
    from api.services.gym_profiles import put_gym_profile
    from api.services.equipment_preferences import put_equipment_preference
    foreign_gym = uuid.uuid4()
    foreign_owner = equipment_exercises[2].app_user_id
    await put_gym_profile(db, foreign_owner, foreign_gym, GymProfilePayload.model_validate(gym_payload()))
    payload = preference()
    foreign = await put_equipment_preference(db, foreign_owner, foreign_gym, "global", equipment_exercises[0].id,
                                             EquipmentPreferencePayload.model_validate(payload))
    foreign_id, foreign_revision = foreign.id, foreign.revision
    own_gym = str(uuid.uuid4())
    assert (await client.put(f"/gym-profiles/{own_gym}", headers=auth_headers, json=gym_payload())).status_code == 200
    hidden = await client.put(f"/gym-profiles/{own_gym}/equipment-preferences/global/{equipment_exercises[0].id}",
                              headers=auth_headers, json=payload)
    assert hidden.status_code == 404 and str(foreign_id) not in hidden.text
    assert (await client.get(f"/gym-profiles/{own_gym}/equipment-preferences", headers=auth_headers)).json() == []
    await db.refresh(foreign)
    assert foreign.revision == foreign_revision


@pytest.mark.asyncio
async def test_equipment_preference_concurrent_create_and_stale_update(client, auth_headers, equipment_exercises):
    import asyncio
    gym_id = str(uuid.uuid4())
    assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers, json=gym_payload())).status_code == 200
    path = f"/gym-profiles/{gym_id}/equipment-preferences/global/{equipment_exercises[0].id}"
    first, duplicate = await asyncio.gather(
        client.put(path, headers=auth_headers, json=preference()),
        client.put(path, headers=auth_headers, json=preference()),
    )
    assert sorted([first.status_code, duplicate.status_code]) == [200, 409]
    saved = first.json() if first.status_code == 200 else duplicate.json()
    rows = (await client.get(f"/gym-profiles/{gym_id}/equipment-preferences", headers=auth_headers)).json()
    assert rows == [saved]
    changed = preference(row_id=saved["id"], bar={"weight": 15, "unit": "kg"}, revision=1)
    updates = await asyncio.gather(*[client.put(path, headers=auth_headers, json=changed) for _ in range(2)])
    assert all(r.status_code == 200 and r.json()["revision"] == 2 for r in updates)
    conflict = await client.put(path, headers=auth_headers, json=preference(row_id=saved["id"], revision=1))
    assert conflict.status_code == 409 and conflict.json()["detail"]["revision"] == 2


@pytest.mark.asyncio
async def test_equipment_preference_uuid_race_across_gyms_does_not_escape_scope(client, auth_headers, equipment_exercises):
    import asyncio
    gyms = [str(uuid.uuid4()), str(uuid.uuid4())]
    for i, gym_id in enumerate(gyms):
        assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers,
                                json=gym_payload(f"Race gym {i}"))).status_code == 200
    payload = preference()
    results = await asyncio.gather(*[
        client.put(f"/gym-profiles/{gym_id}/equipment-preferences/global/{equipment_exercises[0].id}",
                   headers=auth_headers, json=payload) for gym_id in gyms
    ])
    assert sorted(r.status_code for r in results) == [200, 404]
    rows = [(await client.get(f"/gym-profiles/{gym_id}/equipment-preferences", headers=auth_headers)).json() for gym_id in gyms]
    assert sorted(len(r) for r in rows) == [0, 1]


@pytest.mark.asyncio
async def test_equipment_preference_cross_gym_uuid_reuse_does_not_deadlock(client, auth_headers, equipment_exercises, test_user):
    import asyncio
    gyms = [str(uuid.uuid4()), str(uuid.uuid4())]
    saved = []
    for i, gym_id in enumerate(gyms):
        assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers,
                                json=gym_payload(f"Swap gym {i}"))).status_code == 200
        path = f"/gym-profiles/{gym_id}/equipment-preferences/global/{equipment_exercises[0].id}"
        created = await client.put(path, headers=auth_headers, json=preference())
        assert created.status_code == 200
        saved.append(created.json())
    from app.database import SessionLocal
    from api.schemas.gym_profiles import EquipmentPreferencePayload
    from api.services.equipment_preferences import EquipmentPreferenceNotFound, put_equipment_preference
    ready = asyncio.Event()
    arrived = 0

    class PauseAfterCurrentLock:
        def __init__(self, session):
            self.session = session

        async def execute(self, statement, *args, **kwargs):
            nonlocal arrived
            result = await self.session.execute(statement, *args, **kwargs)
            sql = str(statement)
            if "exercise_equipment_preferences.context_key =" in sql and "FOR UPDATE" in sql:
                arrived += 1
                if arrived == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=3)
            return result

        def __getattr__(self, name):
            return getattr(self.session, name)

    async def reuse(i):
        async with SessionLocal() as session:
            return await put_equipment_preference(PauseAfterCurrentLock(session), test_user.id,
                uuid.UUID(gyms[i]), "global", equipment_exercises[0].id,
                EquipmentPreferencePayload.model_validate(preference(row_id=saved[1-i]["id"], revision=1)))

    results = await asyncio.wait_for(asyncio.gather(reuse(0), reuse(1), return_exceptions=True), timeout=5)
    assert all(isinstance(r, EquipmentPreferenceNotFound) for r in results), results


@pytest.mark.asyncio
async def test_equipment_preference_context_key_length_http_boundary(client, auth_headers, equipment_exercises):
    gym_id = str(uuid.uuid4())
    assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers, json=gym_payload())).status_code == 200
    path = f"/gym-profiles/{gym_id}/equipment-preferences/global/{equipment_exercises[0].id}"
    too_long = await client.put(path, headers=auth_headers, json=preference("x" * 513))
    assert too_long.status_code == 422
    assert (await client.get(f"/gym-profiles/{gym_id}/equipment-preferences", headers=auth_headers)).json() == []
    valid = await client.put(path, headers=auth_headers, json=preference("x" * 512))
    assert valid.status_code == 200 and valid.json()["context_key"] == "x" * 512
