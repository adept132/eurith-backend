"""Offline workout snapshots keep the context recorded with each set."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from api.services.models import (
    AppUser, AppUserProfile, ExerciseLoadPreference, GymExerciseSetup, GymProfile,
    UserExerciseProgressionState, WorkoutSessionSet,
)
from api.services.progression.records_repository import rebuild_records

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
    first["exercises"][0].update(active_load_mode="stack", active_setup_id=str(stack.id))
    response = await client.post("/sync/workouts", json=first)
    assert response.status_code == 200, response.text
    rows = response.json()["workout"]["exercises"][0]["sets"]
    assert [row["load_mode"] for row in rows] == ["stack", "plate_loaded"]
    assert rows[0]["load_snapshot"]["step_value"] == 5
    assert rows[1]["load_snapshot"]["step_value"] == 2.5
    assert rows[0]["shown_target_snapshot"] == {"weight": 42, "reps": 8}
    full = deepcopy(first)
    full["gym_snapshot"] = response.json()["workout"]["gym_snapshot"]
    for item, saved in zip(full["exercises"][0]["sets"], rows):
        item["load_snapshot"] = saved["load_snapshot"]

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
    assert response.json()["workout"]["gym_profile_id"] == str(gym.id)
    assert response.json()["workout"]["exercises"][0]["active_setup_id"] == str(stack.id)

    stack.step_value = 7
    await db.commit()
    response = await client.post("/sync/workouts", json=full)
    assert response.status_code == 200, response.text
    assert response.json()["workout"]["exercises"][0]["sets"][0]["load_snapshot"]["step_value"] == 5
    stack.deleted_at = datetime.now(timezone.utc)
    plate.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    response = await client.post("/sync/workouts", json=full)
    assert response.status_code == 200, response.text
    assert response.json()["workout"]["exercises"][0]["sets"][1]["load_snapshot"]["step_value"] == 2.5

    gym.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    response = await client.post("/sync/workouts", json=full)
    assert response.status_code == 200, response.text
    assert response.json()["workout"]["exercises"][0]["sets"][0]["load_snapshot"]["step_value"] == 5
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


async def test_snapshot_only_omission_and_explicit_null(client, db, test_user, seeded_history):
    gym, stack, _ = await configured(db, test_user.id, seeded_history.id)
    uid = str(uuid4())
    original = snapshot(seeded_history.id, workout_uuid=uid,
        sets=[set_value("snapshot-only-a", 1, "stack", gym, stack),
              set_value("snapshot-only-b", 2, "stack", gym, stack)],
        gym_profile_id=str(gym.id))
    created = await client.post("/sync/workouts", json=original)
    assert created.status_code == 200, created.text
    old = snapshot(seeded_history.id, workout_uuid=uid,
        sets=[{"client_uuid": "snapshot-only-a", "set_number": 1, "weight": 42, "reps": 8}])
    response = await client.post("/sync/workouts", json=old)
    assert response.status_code == 200, response.text
    assert response.json()["workout"]["exercises"][0]["sets"][0]["load_snapshot"] is not None
    cleared = snapshot(seeded_history.id, workout_uuid=uid,
        sets=[{"client_uuid": "snapshot-only-a", "set_number": 1, "weight": 42,
               "reps": 8, "load_snapshot": None}])
    response = await client.post("/sync/workouts", json=cleared)
    assert response.status_code == 200, response.text
    first, second = response.json()["workout"]["exercises"][0]["sets"]
    assert first["load_mode"] == "stack" and first["setup_id"] == str(stack.id)
    assert first["load_snapshot"] is None
    assert second["load_snapshot"] is not None


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


async def test_session_and_active_context_omit_preserves_but_reassignment_validates(
    client, db, test_user, seeded_history,
):
    gym, stack, _ = await configured(db, test_user.id, seeded_history.id)
    uid = str(uuid4())
    original = snapshot(seeded_history.id, workout_uuid=uid,
        gym_profile_id=str(gym.id), sets=[set_value("active-set", 1, "stack", gym, stack)])
    original["exercises"][0].update(active_load_mode="stack", active_setup_id=str(stack.id))
    created = await client.post("/sync/workouts", json=original)
    assert created.status_code == 200, created.text
    old = snapshot(seeded_history.id, workout_uuid=uid,
        sets=[{"client_uuid": "active-set", "set_number": 1, "weight": 45, "reps": 8}])
    preserved = await client.post("/sync/workouts", json=old)
    assert preserved.status_code == 200, preserved.text
    assert preserved.json()["workout"]["gym_profile_id"] == str(gym.id)
    assert preserved.json()["workout"]["exercises"][0]["active_setup_id"] == str(stack.id)

    foreign = AppUser(firebase_uid=f"foreign-{uuid4()}", email=f"foreign-{uuid4()}@example.com")
    db.add(foreign)
    await db.flush()
    foreign_gym, foreign_setup, _ = await configured(db, foreign.id, seeded_history.id)
    invalid_gym = await client.post("/sync/workouts", json={**old, "gym_profile_id": str(foreign_gym.id)})
    assert invalid_gym.status_code == 400, invalid_gym.text
    invalid_active = deepcopy(old)
    invalid_active["exercises"][0]["active_setup_id"] = str(foreign_setup.id)
    assert (await client.post("/sync/workouts", json=invalid_active)).status_code == 400
    await db.delete(foreign)
    await db.commit()


async def test_switching_session_gym_validates_active_setup_but_keeps_completed_set(
    client, db, test_user, seeded_history,
):
    gym_a, stack_a, _ = await configured(db, test_user.id, seeded_history.id)
    gym_b = GymProfile(app_user_id=test_user.id, name=f"New studio {uuid4()}",
                       equipment=[], bars=[], discs=[], steps=[])
    db.add(gym_b)
    await db.commit()
    uid = str(uuid4())
    original = snapshot(seeded_history.id, workout_uuid=uid,
        gym_profile_id=str(gym_a.id), sets=[set_value("historical-set", 1, "stack", gym_a, stack_a)])
    original["exercises"][0].update(active_load_mode="stack", active_setup_id=str(stack_a.id))
    created = await client.post("/sync/workouts", json=original)
    assert created.status_code == 200, created.text
    switched = {**original, "gym_profile_id": str(gym_b.id)}
    partial_switch = {**switched, "exercises": []}
    partial_rejected = await client.post("/sync/workouts", json=partial_switch)
    assert partial_rejected.status_code == 400, partial_rejected.text
    rejected = await client.post("/sync/workouts", json=switched)
    assert rejected.status_code == 400, rejected.text
    switched = deepcopy(switched)
    switched["exercises"][0]["active_setup_id"] = None
    accepted = await client.post("/sync/workouts", json=switched)
    assert accepted.status_code == 200, accepted.text
    detail = accepted.json()["workout"]
    assert detail["gym_profile_id"] == str(gym_b.id)
    assert detail["exercises"][0]["active_setup_id"] is None
    old_set = detail["exercises"][0]["sets"][0]
    assert old_set["gym_profile_id"] == str(gym_a.id)
    assert old_set["setup_id"] == str(stack_a.id)


async def test_cannot_reassign_exercise_with_recorded_sets(
    client, db, test_user, seeded_history, fresh_exercise,
):
    gym, stack, _ = await configured(db, test_user.id, seeded_history.id)
    uid = str(uuid4())
    original = snapshot(seeded_history.id, workout_uuid=uid,
        gym_profile_id=str(gym.id), sets=[set_value("recorded-set", 1, "stack", gym, stack)])
    original["exercises"][0].update(active_load_mode="stack", active_setup_id=str(stack.id))
    created = await client.post("/sync/workouts", json=original)
    assert created.status_code == 200, created.text
    reassigned = deepcopy(original)
    reassigned["exercises"][0]["exercise_id"] = fresh_exercise.id
    rejected = await client.post("/sync/workouts", json=reassigned)
    assert rejected.status_code == 400, rejected.text
    unchanged = await client.get(f"/workouts/{created.json()['workout']['id']}")
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["exercises"][0]["exercise"]["id"] == seeded_history.id


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


@pytest.mark.parametrize("mode,basis,plate_unit", [
    ("stack", "displayed", "kg"), ("plate_loaded", "plates_only", "kg"),
    ("plate_loaded", "plates_only", "lb"),
])
async def test_modern_no_gym_snapshot_reaches_progression_and_records(
    client, db, test_user, fresh_exercise, mode, basis, plate_unit
):
    """The mobile no-gym payload retains its recorded global load context."""
    step_key = "plate_kg" if plate_unit == "kg" else "plate_lb"
    config_key = "plate_config_kg" if plate_unit == "kg" else "plate_config_lbs"
    settings = {"weight_unit": "lbs", "weight_steps": {"block_lb": 10, step_key: 2.5},
        config_key: {"plates": [{"weight": 5, "count": 20}]}}
    profile = AppUserProfile(app_user_id=test_user.id, settings=settings)
    db.add(profile)
    db.add(ExerciseLoadPreference(app_user_id=test_user.id, exercise_source="user",
        exercise_id=fresh_exercise.id, enabled_modes=["stack", "plate_loaded"], preferred_mode=mode))
    await db.commit()
    recorded = {"gym_id": None, "gym_name": None, "setup_id": None, "mode": mode,
        "step_value": 10 if mode == "stack" else 2.5,
        "step_unit": "lb" if mode == "stack" else plate_unit,
        "loading_sides": 1 if mode == "stack" else 2, "weight_basis": basis,
        "base_weight": None,
        "plates": None if mode == "stack" and plate_unit == "kg" else [
            {"weight": 5, "count": 20, "unit": plate_unit}]}
    item = {"client_uuid": str(uuid4()), "set_number": 1, "set_type": "normal",
        "weight": 40, "reps": 10, "effort_level": "medium", "is_completed": True,
        "load_mode": mode, "gym_profile_id": None, "setup_id": None,
        "load_snapshot": recorded,
        "shown_target_snapshot": {"set_number": 1, "weight_kg": 40,
            "rep_min": 8, "rep_max": 12, "rir": 2, "kind": "normal"}}
    finished = snapshot(fresh_exercise.id, sets=[item], gym_profile_id=None, gym_snapshot=None)
    finished.update(status="finished", started_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat())
    finished["exercises"][0].update(active_load_mode=mode, active_setup_id=None)
    saved = await client.post("/sync/workouts", json=finished)
    assert saved.status_code == 200, saved.text
    workout = saved.json()["workout"]
    assert workout["exercises"][0]["sets"][0]["load_snapshot"] == recorded
    active = snapshot(fresh_exercise.id, gym_profile_id=None, gym_snapshot=None)
    active["exercises"][0].update(active_load_mode=mode, active_setup_id=None)
    next_workout = await client.post("/sync/workouts", json=active)
    assert next_workout.status_code == 200, next_workout.text
    current_id = next_workout.json()["workout"]["exercises"][0]["id"]
    recommendation_url = f"/workout-session-exercises/{current_id}/autoprogression"
    recommendation = await client.get(recommendation_url,
        params={"load_mode": mode, "gym_profile_id": "none"})
    assert recommendation.status_code == 200, recommendation.text
    assert recommendation.json()["has_basis"] is True
    assert recommendation.json()["target_weight"] > 0
    state = (await db.execute(select(UserExerciseProgressionState).where(
        UserExerciseProgressionState.app_user_id == test_user.id,
        UserExerciseProgressionState.exercise_id == fresh_exercise.id))).scalar_one()
    key = f"gym:none|mode:{mode}|basis:{basis}"
    assert state.records["variants"][key]["weight_at_reps"]["10"]["weight"] == 40
    history = await client.get(f"/exercises/{fresh_exercise.id}/history/{workout['id']}")
    assert history.status_code == 200, history.text
    assert history.json()["sets"][0]["weight_basis"] == basis
    # A full replay preserves the old snapshot after global settings change.
    profile.settings = {**settings, "weight_steps": {"block_lb": 20, step_key: 5}}
    await db.commit()
    replay = await client.post("/sync/workouts", json=finished)
    assert replay.status_code == 200, replay.text
    assert replay.json()["workout"]["exercises"][0]["sets"][0]["load_snapshot"] == recorded
    # Literal null remains an explicit clear, and the unknown basis stays excluded.
    cleared = deepcopy(finished)
    cleared["exercises"][0]["sets"][0]["load_snapshot"] = None
    response = await client.post("/sync/workouts", json=cleared)
    assert response.status_code == 200, response.text
    assert response.json()["workout"]["exercises"][0]["sets"][0]["load_snapshot"] is None
    await rebuild_records(db, test_user.id, [fresh_exercise.id])
    await db.commit()
    await db.refresh(state)
    assert key not in state.records["variants"]
    unknown = await client.get(recommendation_url,
        params={"load_mode": mode, "gym_profile_id": "none"})
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["has_basis"] is False
