"""Requested machine mode recomputes against its own history and equipment."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from api.services.models import (
    AppUser, AppUserProfile, ExerciseLoadPreference, GymExerciseSetup, GymProfile, WorkoutSession,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("gym_has_plates", [False, True])
async def test_explicit_no_gym_uses_global_inventory_and_no_gym_history(
    client, db, test_user, seeded_history, gym_has_plates
):
    """Local 'no gym' must override a server workout that still names a gym."""
    gym = GymProfile(app_user_id=test_user.id, name=f"Old gym {uuid4()}",
        equipment=[], bars=[], steps=[],
        discs=[{"weight": 5, "count": 40, "unit": "kg"}] if gym_has_plates else [])
    db.add(gym)
    db.add(AppUserProfile(app_user_id=test_user.id, settings={
        "weight_steps": {"plate_kg": 2.5},
        "plate_config_kg": {"plates": [{"weight": 5, "count": 20}]},
    }))
    db.add(ExerciseLoadPreference(app_user_id=test_user.id, exercise_source="user",
        exercise_id=seeded_history.id, enabled_modes=["plate_loaded"],
        preferred_mode="plate_loaded"))
    await db.flush()
    no_gym_sets = (await db.execute(select(WorkoutSessionSet).join(WorkoutSessionExercise)
        .where(WorkoutSessionExercise.exercise_id == seeded_history.id))).scalars().all()
    for item in no_gym_sets:
        item.load_mode = "plate_loaded"
        item.load_snapshot = {"weight_basis": "plates_only"}
        item.shown_target_snapshot = {"set_number": item.set_number, "weight_kg": 40,
            "rep_min": 8, "rep_max": 12, "rir": 2, "kind": "normal"}
    named_history = WorkoutSession(app_user_id=test_user.id, source="free", status="finished",
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=1), gym_profile_id=gym.id)
    pending = WorkoutSession(app_user_id=test_user.id, source="free", status="active", gym_profile_id=gym.id)
    no_gym = WorkoutSession(app_user_id=test_user.id, source="free", status="active")
    db.add_all([named_history, pending, no_gym])
    await db.flush()
    previous = WorkoutSessionExercise(workout_session_id=named_history.id,
        exercise_id=seeded_history.id, order_index=0)
    current = WorkoutSessionExercise(workout_session_id=pending.id,
        exercise_id=seeded_history.id, order_index=0)
    reference = WorkoutSessionExercise(workout_session_id=no_gym.id,
        exercise_id=seeded_history.id, order_index=0)
    db.add_all([previous, current, reference])
    await db.flush()
    db.add(WorkoutSessionSet(workout_session_exercise_id=previous.id, set_number=1,
        set_type="normal", weight=200, reps=12, effort_level="medium", is_completed=True,
        load_mode="plate_loaded", gym_profile_id=gym.id,
        load_snapshot={"weight_basis": "plates_only"},
        shown_target_snapshot={"set_number": 1, "weight_kg": 200, "rep_min": 8,
            "rep_max": 12, "rir": 2, "kind": "normal"}))
    await db.commit()

    url = f"/workout-session-exercises/{current.id}/autoprogression"
    implicit = await client.get(url, params={"load_mode": "plate_loaded"})
    assert implicit.status_code == 200, implicit.text
    if gym_has_plates:
        assert implicit.json()["target_weight"] >= 100
    else:
        assert implicit.json()["reason_code"] == "no_reachable_weight"

    expected = await client.get(f"/workout-session-exercises/{reference.id}/autoprogression",
        params={"load_mode": "plate_loaded"})
    assert expected.status_code == 200, expected.text
    assert expected.json()["has_basis"] is True
    assert 0 < expected.json()["target_weight"] < 60
    explicit = await client.get(url, params={"load_mode": "plate_loaded", "gym_profile_id": "none"})
    assert explicit.status_code == 200, explicit.text
    assert explicit.json() == expected.json()
    await db.refresh(pending)
    assert pending.gym_profile_id == gym.id


@pytest.mark.asyncio
async def test_no_gym_query_retains_context_validation(client, db, test_user, seeded_history):
    active = WorkoutSession(app_user_id=test_user.id, source="free", status="active")
    db.add(active)
    await db.flush()
    current = WorkoutSessionExercise(workout_session_id=active.id,
        exercise_id=seeded_history.id, order_index=0)
    foreign_user = AppUser(firebase_uid=f"foreign-{uuid4()}", email=f"foreign-{uuid4()}@example.com")
    db.add_all([current, foreign_user])
    await db.flush()
    foreign_gym = GymProfile(app_user_id=foreign_user.id, name="Foreign", equipment=[], bars=[], discs=[], steps=[])
    db.add(foreign_gym)
    await db.commit()
    url = f"/workout-session-exercises/{current.id}/autoprogression"
    try:
        assert (await client.get(url, params={"gym_profile_id": "none"})).status_code == 400
        for invalid in ("null", "", "not-a-uuid"):
            assert (await client.get(url, params={"load_mode": "stack", "gym_profile_id": invalid})).status_code == 422
        for unavailable in (str(foreign_gym.id), str(uuid4())):
            assert (await client.get(url, params={"load_mode": "stack", "gym_profile_id": unavailable})).status_code == 400
        assert (await client.get(url, params={"load_mode": "stack", "gym_profile_id": "none",
            "setup_id": str(uuid4())})).status_code == 400
    finally:
        await db.delete(foreign_user)
        await db.commit()
