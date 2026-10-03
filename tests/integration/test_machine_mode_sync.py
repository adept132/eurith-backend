"""Offline workout snapshots keep the context recorded with each set."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from api.services.models import AppUser, ExerciseLoadPreference, GymExerciseSetup, GymProfile, WorkoutSessionSet

pytestmark = pytest.mark.asyncio


def snapshot(exercise_id, *, workout_uuid=None, sets=None, **context):
    return {
        "client_uuid": workout_uuid or str(uuid4()), "source": "free", "status": "active",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "exercises": [{"client_uuid": "exercise-1", "exercise_id": exercise_id,
                       "order_index": 0, "sets": sets or []}], **context,
    }


def set_value(name, number, mode=None, gym=None, setup=None):
    item = {"client_uuid": name, "set_number": number, "weight": 40, "reps": 8,
            "shown_target_snapshot": {"weight": 42, "reps": 8}}
    if mode is not None:
        item.update(load_mode=mode, gym_profile_id=str(gym.id), setup_id=str(setup.id))
    return item


async def configured(db, owner_id, exercise_id):
    gym = GymProfile(app_user_id=owner_id, name=f"Studio {uuid4()}",
                     equipment=[], bars=[], discs=[], steps=[])
    db.add(gym)
    await db.flush()
    db.add(ExerciseLoadPreference(app_user_id=owner_id, exercise_source="user",
        exercise_id=exercise_id, enabled_modes=["stack", "plate_loaded"], preferred_mode="stack"))
    stack = GymExerciseSetup(gym_id=gym.id, exercise_source="user", exercise_id=exercise_id,
        load_mode="stack", is_available=True, step_value=5, step_unit="kg",
        loading_sides=1, weight_basis="displayed")
    plate = GymExerciseSetup(gym_id=gym.id, exercise_source="user", exercise_id=exercise_id,
        load_mode="plate_loaded", is_available=True, step_value=2.5, step_unit="kg",
        loading_sides=2, weight_basis="plates_only")
    db.add_all([stack, plate])
    await db.commit()
    return gym, stack, plate


async def test_modes_survive_replay_legacy_payload_conflict_and_deleted_gym(
    client, db, test_user, seeded_history,
):
    gym, stack, plate = await configured(db, test_user.id, seeded_history.id)
    uid = str(uuid4())
    first = snapshot(seeded_history.id, workout_uuid=uid,
        sets=[set_value("stack-set", 1, "stack", gym, stack),
              set_value("plate-set", 2, "plate_loaded", gym, plate)],
        gym_profile_id=str(gym.id))
    response = await client.post("/sync/workouts", json=first)
    assert response.status_code == 200, response.text
    rows = response.json()["workout"]["exercises"][0]["sets"]
    assert [row["load_mode"] for row in rows] == ["stack", "plate_loaded"]
    assert rows[0]["load_snapshot"]["step_value"] == 5
    assert rows[1]["load_snapshot"]["step_value"] == 2.5
    assert rows[0]["shown_target_snapshot"] == {"weight": 42, "reps": 8}

    replay = await client.post("/sync/workouts", json=first)
    assert replay.status_code == 200, replay.text
    assert [row["id"] for row in replay.json()["workout"]["exercises"][0]["sets"]] == [row["id"] for row in rows]
    legacy = snapshot(seeded_history.id, workout_uuid=uid,
        sets=[{"client_uuid": "stack-set", "set_number": 1, "weight": 45, "reps": 8},
              {"client_uuid": "plate-set", "set_number": 2, "weight": 45, "reps": 8}])
    response = await client.post("/sync/workouts", json=legacy)
    assert response.status_code == 200, response.text
    rows = response.json()["workout"]["exercises"][0]["sets"]
    assert [row["load_mode"] for row in rows] == ["stack", "plate_loaded"]
    assert all(row["shown_target_snapshot"] == {"weight": 42, "reps": 8} for row in rows)

    gym.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    response = await client.post("/sync/workouts", json=legacy)
    assert response.status_code == 200, response.text
    assert response.json()["workout"]["exercises"][0]["sets"][0]["load_snapshot"]["step_value"] == 5
    conflict = await client.post("/sync/workouts", json={**legacy, "base_version": 1})
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["workout"]["exercises"][0]["sets"][1]["load_mode"] == "plate_loaded"
    pulled = await client.get("/sync/changes")
    assert pulled.status_code == 200, pulled.text
    assert any(w["client_uuid"] == uid and w["exercises"][0]["sets"][0]["load_snapshot"] for w in pulled.json()["workouts"])
    count = (await db.execute(select(WorkoutSessionSet).where(WorkoutSessionSet.client_uuid.in_(["stack-set", "plate-set"])))).scalars().all()
    assert len(count) == 2


async def test_sync_rejects_foreign_gym_and_setup(client, db, test_user, seeded_history):
    gym, stack, _ = await configured(db, test_user.id, seeded_history.id)
    foreign = AppUser(firebase_uid=f"foreign-{uuid4()}", email=f"foreign-{uuid4()}@example.com")
    db.add(foreign)
    await db.flush()
    foreign_gym, foreign_setup, _ = await configured(db, foreign.id, seeded_history.id)
    for item in (set_value("foreign-gym", 1, "stack", foreign_gym, foreign_setup),
                 set_value("foreign-setup", 1, "stack", gym, foreign_setup)):
        response = await client.post("/sync/workouts", json=snapshot(seeded_history.id, sets=[item]))
        assert response.status_code == 400, response.text
    await db.delete(foreign)
    await db.commit()


async def test_explicit_set_edit_changes_only_that_set(client, db, test_user, seeded_history):
    gym, stack, plate = await configured(db, test_user.id, seeded_history.id)
    uid = str(uuid4())
    original = snapshot(seeded_history.id, workout_uuid=uid,
        sets=[set_value("edit-a", 1, "stack", gym, stack),
              set_value("edit-b", 2, "stack", gym, stack)])
    created = await client.post("/sync/workouts", json=original)
    assert created.status_code == 200, created.text
    edited = set_value("edit-b", 2, "plate_loaded", gym, plate)
    edited["shown_target_snapshot"] = {"weight": 55, "reps": 6}
    changed = await client.post("/sync/workouts", json=snapshot(
        seeded_history.id, workout_uuid=uid,
        sets=[{"client_uuid": "edit-a", "set_number": 1, "weight": 40, "reps": 8}, edited],
    ))
    assert changed.status_code == 200, changed.text
    first, second = changed.json()["workout"]["exercises"][0]["sets"]
    assert first["load_mode"] == "stack"
    assert first["setup_id"] == str(stack.id)
    assert first["shown_target_snapshot"] == {"weight": 42, "reps": 8}
    assert second["load_mode"] == "plate_loaded"
    assert second["setup_id"] == str(plate.id)
    assert second["shown_target_snapshot"] == {"weight": 55, "reps": 6}
    cleared = await client.post("/sync/workouts", json=snapshot(
        seeded_history.id, workout_uuid=uid,
        sets=[{"client_uuid": "edit-b", "set_number": 2, "weight": 40,
               "reps": 8, "load_mode": None, "shown_target_snapshot": None}],
    ))
    assert cleared.status_code == 200, cleared.text
    rows = cleared.json()["workout"]["exercises"][0]["sets"]
    assert rows[0]["load_mode"] == "stack"
    assert rows[1]["load_mode"] is None
    assert rows[1]["setup_id"] is None
    assert rows[1]["shown_target_snapshot"] is None
