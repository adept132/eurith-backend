"""Direct workout set writes keep per-set machine context historical."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from api.services.models import (
    AppUser, AppUserProfile, ExerciseLoadPreference, GymExerciseSetup, GymProfile,
    WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet,
)


async def _workout(client, headers, exercise_id):
    started = await client.post("/workouts/start", headers=headers, json={"source": "free"})
    assert started.status_code == 200, started.text
    workout_id = started.json()["id"]
    added = await client.post(f"/workouts/{workout_id}/exercises", headers=headers,
                              json={"exercise_id": exercise_id})
    assert added.status_code == 200, added.text
    return workout_id, added.json()["exercises"][-1]["id"]


async def _configured(db, user_id, exercise_id):
    gym = GymProfile(app_user_id=user_id, name="Studio", equipment=[], bars=[], discs=[], steps=[])
    db.add(gym)
    await db.flush()
    preference = ExerciseLoadPreference(app_user_id=user_id, exercise_source="user",
        exercise_id=exercise_id, enabled_modes=["stack", "plate_loaded"], preferred_mode="stack")
    stack = GymExerciseSetup(gym_id=gym.id, exercise_source="user", exercise_id=exercise_id,
        load_mode="stack", is_available=True, step_value=5, step_unit="kg",
        loading_sides=1, weight_basis="displayed")
    plate = GymExerciseSetup(gym_id=gym.id, exercise_source="user", exercise_id=exercise_id,
        load_mode="plate_loaded", is_available=True, step_value=2.5, step_unit="kg",
        loading_sides=2, weight_basis="plates_only")
    db.add_all([preference, stack, plate])
    await db.commit()
    return gym, stack, plate


@pytest.mark.asyncio
async def test_legacy_gym_uses_custom_global_stack_step_and_accepts_its_snapshot(
    client, auth_headers, db, test_user, seeded_history,
):
    profile = (await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == test_user.id))).scalar_one_or_none()
    if profile is None:
        profile = AppUserProfile(app_user_id=test_user.id, settings={})
        db.add(profile)
    profile.settings = {**(profile.settings or {}), "weight_steps": {"block_lb": 7.5}}
    db.add(ExerciseLoadPreference(app_user_id=test_user.id, exercise_source="user",
        exercise_id=seeded_history.id, enabled_modes=["stack"], preferred_mode="stack"))
    await db.commit()
    _, exercise_id = await _workout(client, auth_headers, seeded_history.id)
    url = f"/workout-session-exercises/{exercise_id}/sets"
    first = await client.post(url, headers=auth_headers,
        json={"load_mode": "stack", "weight": 40})
    assert first.status_code == 200, first.text
    snapshot = first.json()["load_snapshot"]
    assert snapshot["step_value"] == 7.5
    assert snapshot["step_unit"] == "lb"
    replay = await client.post(url, headers=auth_headers,
        json={"load_mode": "stack", "load_snapshot": snapshot, "weight": 45})
    assert replay.status_code == 200, replay.text
    assert replay.json()["load_snapshot"] == snapshot


@pytest.mark.asyncio
async def test_legacy_gym_uses_custom_global_plate_inventory(
    client, auth_headers, db, test_user, seeded_history,
):
    profile = (await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == test_user.id))).scalar_one_or_none()
    if profile is None:
        profile = AppUserProfile(app_user_id=test_user.id, settings={})
        db.add(profile)
    profile.settings = {**(profile.settings or {}),
        "weight_steps": {"plate_kg": 1.25},
        "plate_config_kg": {"barWeights": [20], "selectedBar": 20,
                            "plates": [{"weight": 1.25, "count": 4}]}}
    db.add(ExerciseLoadPreference(app_user_id=test_user.id, exercise_source="user",
        exercise_id=seeded_history.id, enabled_modes=["plate_loaded"],
        preferred_mode="plate_loaded"))
    await db.commit()
    _, exercise_id = await _workout(client, auth_headers, seeded_history.id)
    response = await client.post(f"/workout-session-exercises/{exercise_id}/sets",
        headers=auth_headers, json={"load_mode": "plate_loaded", "weight": 20})
    assert response.status_code == 200, response.text
    snapshot = response.json()["load_snapshot"]
    assert snapshot["step_value"] == 1.25
    assert snapshot["step_unit"] == "kg"
    assert snapshot["plates"] == [{"weight": 1.25, "count": 4, "unit": "kg"}]


@pytest.mark.asyncio
async def test_direct_add_patch_and_repeat_keep_historical_context(
    client, auth_headers, db, test_user, seeded_history,
):
    gym, stack, plate = await _configured(db, test_user.id, seeded_history.id)
    profile = (await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == test_user.id))).scalar_one_or_none()
    if profile is None:
        profile = AppUserProfile(app_user_id=test_user.id, settings={})
        db.add(profile)
    profile.settings = {**(profile.settings or {}), "weight_steps": {
        "block_lb": 7.5, "plate_kg": 1.25,
    }, "plate_config_kg": [{"weight": 1.25, "count": 4}]}
    await db.commit()
    workout_id, exercise_id = await _workout(client, auth_headers, seeded_history.id)
    workout = await db.get(WorkoutSession, workout_id)
    exercise = await db.get(WorkoutSessionExercise, exercise_id)
    workout.gym_profile_id = gym.id
    workout.gym_snapshot = {"name": "Studio"}
    exercise.active_load_mode = "stack"
    exercise.active_setup_id = stack.id
    await db.commit()

    url = f"/workout-session-exercises/{exercise_id}/sets"
    first = await client.post(url, headers=auth_headers,
        json={"weight": 40, "reps": 8, "shown_target_snapshot": {"weight": 42.5}})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["load_mode"] == "stack"
    assert first_body["setup_id"] == str(stack.id)
    assert first_body["gym_profile_id"] == str(gym.id)
    assert first_body["load_snapshot"]["weight_basis"] == "displayed"
    assert first_body["load_snapshot"]["step_value"] == 5
    assert first_body["load_snapshot"]["step_unit"] == "kg"
    assert first_body["shown_target_snapshot"] == {"weight": 42.5}

    exercise.active_load_mode = "plate_loaded"
    exercise.active_setup_id = plate.id
    await db.commit()
    second = await client.post(url, headers=auth_headers, json={"weight": 25, "reps": 10})
    assert second.status_code == 200, second.text
    assert second.json()["load_mode"] == "plate_loaded"
    assert second.json()["load_snapshot"]["weight_basis"] == "plates_only"
    assert second.json()["load_snapshot"]["step_value"] == 2.5

    first_id = first_body["id"]
    patched = await client.patch(f"/workout-session-sets/{first_id}", headers=auth_headers,
                                 json={"reps": 9})
    assert patched.status_code == 200, patched.text
    assert patched.json()["load_mode"] == "stack"
    assert patched.json()["load_snapshot"] == first_body["load_snapshot"]
    assert patched.json()["shown_target_snapshot"] == {"weight": 42.5}

    repeated = await client.post(f"/workout-session-sets/{first_id}/repeat", headers=auth_headers,
                                 json={})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["load_snapshot"] == first_body["load_snapshot"]
    assert repeated.json()["shown_target_snapshot"] == {"weight": 42.5}
    original = await db.get(WorkoutSessionSet, first_id)
    copy = await db.get(WorkoutSessionSet, repeated.json()["id"])
    assert original.load_snapshot is not copy.load_snapshot
    assert original.shown_target_snapshot is not copy.shown_target_snapshot

    gym.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    historical_edit = await client.patch(f"/workout-session-sets/{first_id}",
        headers=auth_headers, json={"reps": 11})
    assert historical_edit.status_code == 200, historical_edit.text
    assert historical_edit.json()["load_snapshot"] == first_body["load_snapshot"]
    detail = await client.get(f"/workouts/{workout_id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["exercises"][0]["sets"][0]["load_snapshot"] == first_body["load_snapshot"]


@pytest.mark.asyncio
async def test_explicit_one_session_mode_and_old_set_reassignment(
    client, auth_headers, db, test_user, seeded_history,
):
    gym, stack, plate = await _configured(db, test_user.id, seeded_history.id)
    plate.deleted_at = datetime.now(timezone.utc)
    await db.commit()
    workout_id, exercise_id = await _workout(client, auth_headers, seeded_history.id)
    workout = await db.get(WorkoutSession, workout_id)
    workout.gym_profile_id = gym.id
    await db.commit()
    url = f"/workout-session-exercises/{exercise_id}/sets"
    created = await client.post(url, headers=auth_headers,
        json={"load_mode": "plate_loaded", "setup_id": None, "weight": 30, "reps": 8})
    assert created.status_code == 200, created.text
    assert created.json()["load_mode"] == "plate_loaded"
    assert created.json()["setup_id"] is None
    assert created.json()["load_snapshot"]["weight_basis"] == "plates_only"
    available_plate_setups = (await db.execute(select(GymExerciseSetup).where(
        GymExerciseSetup.gym_id == gym.id,
        GymExerciseSetup.load_mode == "plate_loaded",
        GymExerciseSetup.deleted_at.is_(None),
    ))).scalars().all()
    assert available_plate_setups == []
    changed = await client.patch(f"/workout-session-sets/{created.json()['id']}",
        headers=auth_headers, json={"load_mode": "stack", "setup_id": str(stack.id)})
    assert changed.status_code == 200, changed.text
    assert Decimal(changed.json()["weight"]) == 30
    assert changed.json()["load_snapshot"]["weight_basis"] == "displayed"


@pytest.mark.asyncio
async def test_forged_context_and_cross_exercise_repeat_are_rejected(
    client, auth_headers, db, test_user, seeded_history, fresh_exercise,
):
    gym, stack, _ = await _configured(db, test_user.id, seeded_history.id)
    foreign = AppUser(firebase_uid=f"foreign-{uuid4()}", email=f"foreign-{uuid4()}@example.com")
    db.add(foreign)
    await db.flush()
    foreign_gym = GymProfile(app_user_id=foreign.id, name="Foreign", equipment=[], bars=[], discs=[], steps=[])
    db.add(foreign_gym)
    await db.flush()
    foreign_setup = GymExerciseSetup(gym_id=foreign_gym.id, exercise_source="user",
        exercise_id=seeded_history.id, load_mode="stack", is_available=True,
        step_value=5, step_unit="kg", loading_sides=1, weight_basis="displayed")
    wrong_exercise_setup = GymExerciseSetup(gym_id=gym.id, exercise_source="user",
        exercise_id=fresh_exercise.id, load_mode="stack", is_available=True,
        step_value=5, step_unit="kg", loading_sides=1, weight_basis="displayed")
    db.add_all([foreign_setup, wrong_exercise_setup])
    await db.commit()
    workout_id, exercise_id = await _workout(client, auth_headers, seeded_history.id)
    workout = await db.get(WorkoutSession, workout_id)
    workout.gym_profile_id = gym.id
    await db.commit()
    url = f"/workout-session-exercises/{exercise_id}/sets"
    for bad in ({"load_mode": "stack", "setup_id": str(foreign_setup.id)},
                {"load_mode": "stack", "gym_profile_id": str(foreign_gym.id)},
                {"load_mode": "stack", "setup_id": str(wrong_exercise_setup.id)},
                {"load_mode": "plate_loaded", "setup_id": str(stack.id)},
                {"load_mode": "stack", "setup_id": str(stack.id),
                 "load_snapshot": {"weight_basis": "plates_only"}}):
        response = await client.post(url, headers=auth_headers, json={"weight": 20, **bad})
        assert response.status_code == 400, response.text
    valid = await client.post(url, headers=auth_headers,
        json={"load_mode": "stack", "setup_id": str(stack.id), "weight": 20})
    assert valid.status_code == 200, valid.text
    other = await client.post(f"/workouts/{workout_id}/exercises", headers=auth_headers,
                              json={"exercise_id": fresh_exercise.id})
    assert other.status_code == 200, other.text
    other_id = other.json()["exercises"][-1]["id"]
    repeat = await client.post(f"/workout-session-sets/{valid.json()['id']}/repeat",
        headers=auth_headers, json={"target_session_exercise_id": other_id})
    assert repeat.status_code == 400, repeat.text
    await db.delete(foreign)
    await db.commit()
