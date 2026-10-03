"""A started workout retains the confirmed plan's gym and machine choice."""

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from api.services.models import (
    AppUserProfile, GymProfile, WorkoutPlanExercise, WorkoutSessionExercise,
    WorkoutSessionSet,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_path", ["apply", "workouts_start"])
async def test_plan_start_reads_saved_gym_and_mode_after_active_gym_changes(
    client, auth_headers, db, test_user, seeded_plan, start_path,
):
    gym = GymProfile(app_user_id=test_user.id, name="Studio", equipment=["block_machine"],
                     bars=[], discs=[], steps=[])
    other_gym = GymProfile(app_user_id=test_user.id, name="Other", equipment=[],
                           bars=[], discs=[], steps=[])
    db.add_all([gym, other_gym])
    await db.flush()
    setup_id = uuid4()
    seeded_plan.gym_profile_id = gym.id
    seeded_plan.gym_snapshot = {"name": "Confirmed Studio", "equipment": ["block_machine"]}
    plan_ex = (await db.execute(select(WorkoutPlanExercise).where(
        WorkoutPlanExercise.plan_id == seeded_plan.id))).scalar_one()
    plan_ex.load_mode = "stack"
    plan_ex.setup_id = setup_id
    plan_ex.load_snapshot = {"mode": "stack", "step_value": 2.5}
    await db.commit()

    if start_path == "apply":
        response = await client.post(f"/plans/{seeded_plan.id}/apply", headers=auth_headers,
            json={"apply_mode": "today", "target_date": date.today().isoformat(),
                  "day_tag": "push", "micro_tag": "medium"})
        session_id = response.json().get("session_id")
    else:
        response = await client.post("/workouts/start", headers=auth_headers,
                                     json={"source": "free", "plan_id": seeded_plan.id})
        session_id = response.json().get("id")
    assert response.status_code == 200, response.text

    profile = (await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == test_user.id))).scalar_one_or_none()
    if profile is None:
        profile = AppUserProfile(app_user_id=test_user.id, settings={})
        db.add(profile)
    profile.settings = {**(profile.settings or {}), "active_gym_profile_id": str(other_gym.id)}
    gym.deleted_at = datetime.now(timezone.utc)
    await db.commit()

    for path in (f"/workouts/{session_id}", "/workouts/active"):
        detail_response = await client.get(path, headers=auth_headers)
        assert detail_response.status_code == 200, detail_response.text
        detail = detail_response.json()
        assert detail["gym_profile_id"] == str(gym.id)
        assert detail["gym_snapshot"] == {"name": "Confirmed Studio", "equipment": ["block_machine"]}
        exercise = detail["exercises"][0]
        assert exercise["active_load_mode"] == "stack"
        assert exercise["active_setup_id"] == str(setup_id)


@pytest.mark.asyncio
async def test_detail_reads_per_set_tags_and_legacy_nulls(
    client, auth_headers, db, seeded_plan,
):
    response = await client.post(f"/plans/{seeded_plan.id}/apply", headers=auth_headers,
        json={"apply_mode": "today", "target_date": date.today().isoformat(),
              "day_tag": "push", "micro_tag": "medium"})
    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]
    exercise = (await db.execute(select(WorkoutSessionExercise).where(
        WorkoutSessionExercise.workout_session_id == session_id))).scalar_one()
    first_set = (await db.execute(select(WorkoutSessionSet).where(
        WorkoutSessionSet.workout_session_exercise_id == exercise.id,
        WorkoutSessionSet.set_number == 1))).scalar_one()
    gym_id, setup_id = uuid4(), uuid4()
    first_set.load_mode = "plate_loaded"
    first_set.gym_profile_id = gym_id
    first_set.setup_id = setup_id
    first_set.load_snapshot = {"weight_basis": "plates_only"}
    first_set.shown_target_snapshot = {"weight": 25}
    await db.commit()

    detail_response = await client.get(f"/workouts/{session_id}", headers=auth_headers)
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["gym_profile_id"] is None and detail["gym_snapshot"] is None
    assert detail["exercises"][0]["active_load_mode"] is None
    assert detail["exercises"][0]["active_setup_id"] is None
    tagged, legacy = detail["exercises"][0]["sets"][:2]
    assert tagged["load_mode"] == "plate_loaded"
    assert tagged["gym_profile_id"] == str(gym_id)
    assert tagged["setup_id"] == str(setup_id)
    assert tagged["load_snapshot"] == {"weight_basis": "plates_only"}
    assert tagged["shown_target_snapshot"] == {"weight": 25}
    assert all(legacy[key] is None for key in (
        "load_mode", "gym_profile_id", "setup_id", "load_snapshot", "shown_target_snapshot"))
