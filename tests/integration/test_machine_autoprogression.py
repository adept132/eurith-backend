"""Requested machine mode recomputes against its own history and equipment."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from api.services.models import (
    ExerciseLoadPreference, GymExerciseSetup, GymProfile, WorkoutSession,
    WorkoutSessionExercise, WorkoutSessionSet,
)
from api.services.progression.types import Prescription, SetPrescription


@pytest.mark.asyncio
async def test_requested_modes_bypass_stored_goal_and_reject_foreign_setup(client, db, test_user, seeded_history):
    gym = GymProfile(app_user_id=test_user.id, name=f"Gym {uuid4()}", equipment=[], bars=[],
                     discs=[{"weight": 5, "count": 4, "unit": "kg"}], steps=[])
    db.add(gym)
    await db.flush()
    db.add(ExerciseLoadPreference(app_user_id=test_user.id, exercise_source="user",
        exercise_id=seeded_history.id, enabled_modes=["stack", "plate_loaded"], preferred_mode="stack"))
    stack = GymExerciseSetup(gym_id=gym.id, exercise_source="user", exercise_id=seeded_history.id,
        load_mode="stack", is_available=True, step_value=5, step_unit="lb",
        loading_sides=1, weight_basis="displayed")
    plate = GymExerciseSetup(gym_id=gym.id, exercise_source="user", exercise_id=seeded_history.id,
        load_mode="plate_loaded", is_available=True, step_value=5, step_unit="kg",
        loading_sides=2, weight_basis="plates_only")
    db.add_all([stack, plate])
    await db.flush()
    old_sets = (await db.execute(select(WorkoutSessionSet).join(WorkoutSessionExercise).where(
        WorkoutSessionExercise.exercise_id == seeded_history.id))).scalars().all()
    for item in old_sets:
        item.load_mode, item.gym_profile_id, item.setup_id = "stack", gym.id, stack.id
        item.load_snapshot = {"weight_basis": "displayed"}
        item.shown_target_snapshot = {"set_number": item.set_number, "weight_kg": 40,
            "rep_min": 8, "rep_max": 12, "rir": 2, "kind": "normal"}
    finished = WorkoutSession(app_user_id=test_user.id, source="free", status="finished",
                              finished_at=datetime.now(timezone.utc))
    active = WorkoutSession(app_user_id=test_user.id, source="free", status="active", gym_profile_id=gym.id)
    db.add_all([finished, active])
    await db.flush()
    prior = WorkoutSessionExercise(workout_session_id=finished.id, exercise_id=seeded_history.id, order_index=0)
    current = WorkoutSessionExercise(workout_session_id=active.id, exercise_id=seeded_history.id, order_index=0)
    db.add_all([prior, current])
    await db.flush()
    db.add(WorkoutSessionSet(workout_session_exercise_id=prior.id, set_number=1, set_type="normal",
        weight=30, reps=10, is_completed=True, load_mode="plate_loaded", gym_profile_id=gym.id,
        setup_id=plate.id, load_snapshot={"weight_basis": "plates_only"},
        shown_target_snapshot={"set_number": 1, "weight_kg": 30, "rep_min": 8,
                               "rep_max": 12, "rir": 2, "kind": "normal"}))
    current.prescription = Prescription(scheme="double", sets=(SetPrescription(1, 100, 8, 12, 2),),
        reason_code="stored", reason_text="old mode").to_dict()
    await db.commit()

    url = f"/workout-session-exercises/{current.id}/autoprogression"
    legacy = await client.get(url)
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["target_weight"] == 100
    stack_result = await client.get(url, params={"load_mode": "stack", "setup_id": str(stack.id)})
    plate_result = await client.get(url, params={"load_mode": "plate_loaded", "setup_id": str(plate.id)})
    assert stack_result.status_code == 200, stack_result.text
    assert plate_result.status_code == 200, plate_result.text
    assert 0 < stack_result.json()["target_weight"] < 100
    assert stack_result.json()["target_weight"] != plate_result.json()["target_weight"]
    assert plate_result.json()["target_weight"] in (10, 20, 30, 40)
    assert current.prescription["sets"][0]["weight_kg"] == 100

    forbidden = await client.get(url, params={"load_mode": "stack", "setup_id": str(plate.id)})
    assert forbidden.status_code == 400
    other_gym = GymProfile(app_user_id=test_user.id, name=f"Other {uuid4()}",
                           equipment=[], bars=[], discs=[], steps=[])
    db.add(other_gym)
    await db.commit()
    moved = await client.get(url, params={"load_mode": "plate_loaded", "gym_profile_id": str(other_gym.id)})
    assert moved.status_code == 200, moved.text
    assert moved.json()["reason_code"] == "no_reachable_weight"
    assert (await client.get(url, params={"load_mode": "plate_loaded", "setup_id": str(plate.id),
                                          "gym_profile_id": str(other_gym.id)})).status_code == 400


@pytest.mark.asyncio
async def test_named_gym_without_plates_returns_explicit_no_target(client, db, test_user, seeded_history):
    gym = GymProfile(app_user_id=test_user.id, name=f"Empty {uuid4()}", equipment=[], bars=[], discs=[], steps=[])
    db.add(gym)
    await db.flush()
    db.add(ExerciseLoadPreference(app_user_id=test_user.id, exercise_source="user",
        exercise_id=seeded_history.id, enabled_modes=["plate_loaded"], preferred_mode="plate_loaded"))
    active = WorkoutSession(app_user_id=test_user.id, source="free", status="active", gym_profile_id=gym.id)
    db.add(active)
    await db.flush()
    current = WorkoutSessionExercise(workout_session_id=active.id, exercise_id=seeded_history.id, order_index=0)
    db.add(current)
    await db.commit()
    response = await client.get(f"/workout-session-exercises/{current.id}/autoprogression",
        params={"load_mode": "plate_loaded"})
    assert response.status_code == 200, response.text
    assert response.json()["target_weight"] is None
    assert response.json()["reason_code"] == "no_reachable_weight"
    assert (await client.get(f"/workout-session-exercises/{current.id}/autoprogression",
        params={"load_mode": "stack"})).status_code == 400
    assert (await client.get(f"/workout-session-exercises/{current.id}/autoprogression",
        params={"setup_id": str(uuid4())})).status_code == 400
