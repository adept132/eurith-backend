"""Confirmation must revalidate context and persist server-owned snapshots."""
import copy
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

from api.routers import plans
from api.schemas.plan import ConfirmPlanRequest, GeneratedDayOut, GeneratedExerciseOut
from api.services.models import (
    Exercise, ExerciseLoadPreference, GymProfile, GymExerciseSetup,
    WorkoutPlan, WorkoutPlanExercise, UserSplit,
)


@pytest_asyncio.fixture
async def scenario(db, test_user):
    gym = GymProfile(app_user_id=test_user.id, name="Studio", equipment=["block_machine"],
                     bars=[{"weight": 20}], discs=[{"weight": 5, "count": 4}], steps=[])
    ex = Exercise(name=f"Machine {uuid.uuid4()}", category="strength", main_muscle_group="chest",
                  secondary_muscle_groups=[], equipment_needed=["block_machine"],
                  difficulty="beginner", source="test", app_user_id=test_user.id)
    db.add_all([gym, ex])
    await db.flush()
    setup = GymExerciseSetup(gym_id=gym.id, exercise_source="user", exercise_id=ex.id,
        load_mode="plate_loaded", is_available=True, step_value=2.5, step_unit="kg",
        loading_sides=2, weight_basis="plates_only", base_weight=20)
    db.add(setup)
    await db.commit()
    day = GeneratedDayOut(day_tag="Push", day_name="Push", schedule_tags=["Push", "Push__2"],
        coverage={}, exercises=[GeneratedExerciseOut(exercise_id=ex.id, name=ex.name,
        target_sets=3, order_index=0, fatigue_tier=2, primary_muscle="Грудь",
        load_mode="plate_loaded", setup_id=setup.id,
        load_snapshot={"gym_id": str(gym.id), "setup_id": str(setup.id), "mode": "plate_loaded",
                       "base_weight": 999, "plates": [{"weight": 999, "count": 999}]})])
    yield SimpleNamespace(db=db, user=test_user, gym=gym, ex=ex, setup=setup, day=day)
    await db.rollback()
    ex.app_user_id = test_user.id
    await db.commit()


async def confirm(s, **kwargs):
    return await plans.confirm_generated_plan(ConfirmPlanRequest(
        days=kwargs.pop("days", [s.day]), gym_profile_id=kwargs.pop("gym_profile_id", s.gym.id),
        **kwargs), s.db, s.user)


@pytest.mark.asyncio
async def test_confirm_persists_server_snapshots_for_each_day(scenario):
    s = scenario
    second = s.day.model_copy(deep=True, update={"day_tag": "Pull", "day_name": "Pull"})
    s.setup.step_value = 5
    await s.db.commit()
    result = await confirm(s, days=[s.day, second])
    assert len(result.created_plan_ids) == 2
    assert result.updated_day_tags == ["Push", "Push__2"]
    for plan_id in result.created_plan_ids:
        plan = await s.db.get(WorkoutPlan, plan_id)
        exercise = (await s.db.execute(select(WorkoutPlanExercise).where(
            WorkoutPlanExercise.plan_id == plan_id))).scalar_one()
        assert plan.gym_profile_id == s.gym.id
        assert plan.gym_snapshot["name"] == "Studio"
        assert plan.gym_snapshot["equipment"] == ["block_machine"]
        assert plan.gym_snapshot["discs"] == [{"weight": 5, "count": 4}]
        assert exercise.setup_id == s.setup.id
        assert exercise.load_mode == "plate_loaded"
        assert exercise.load_snapshot["base_weight"] == 20
        assert exercise.load_snapshot["step_value"] == 5
        assert exercise.load_snapshot["step_unit"] == "kg"
        assert exercise.load_snapshot["loading_sides"] == 2
        assert exercise.load_snapshot["weight_basis"] == "plates_only"
        assert exercise.load_snapshot["plates"] == [{"weight": 5, "count": 4, "unit": "kg"}]
    saved_gym, saved_load = copy.deepcopy(plan.gym_snapshot), copy.deepcopy(exercise.load_snapshot)
    s.gym.name = "Renamed"
    s.gym.discs = []
    s.gym.deleted_at = datetime.now(timezone.utc)
    s.setup.base_weight = 30
    await s.db.commit()
    await s.db.refresh(plan)
    await s.db.refresh(exercise)
    assert plan.gym_snapshot == saved_gym
    assert exercise.load_snapshot == saved_load


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["setup", "mode", "archived", "unavailable", "preference",
    "foreign_gym", "deleted_gym", "foreign_exercise", "missing_mode", "missing_setup", "no_gym",
    "snapshot_gym", "snapshot_setup", "snapshot_mode"])
async def test_confirm_rejects_stale_or_forged_context_before_writing(scenario, change):
    s = scenario
    ex = s.day.exercises[0]
    if change == "setup":
        ex.setup_id = uuid.uuid4()
    elif change == "mode":
        ex.load_mode = "stack"
    elif change == "archived":
        s.setup.deleted_at = datetime.now(timezone.utc)
    elif change == "unavailable":
        s.setup.is_available = False
    elif change == "preference":
        s.db.add(ExerciseLoadPreference(app_user_id=s.user.id, exercise_source="user",
            exercise_id=s.ex.id, enabled_modes=["stack"], preferred_mode="stack"))
    elif change == "foreign_gym":
        s.gym.id = uuid.uuid4()  # Request a nonexistent/foreign identity without changing persisted rows.
        s.db.expunge(s.gym)
    elif change == "deleted_gym":
        s.gym.deleted_at = datetime.now(timezone.utc)
    elif change == "foreign_exercise":
        s.ex.app_user_id = None
    elif change == "missing_mode":
        ex.load_mode = None
    elif change == "missing_setup":
        ex.setup_id = None
    elif change.startswith("snapshot_"):
        key = {"snapshot_gym": "gym_id", "snapshot_setup": "setup_id", "snapshot_mode": "mode"}[change]
        ex.load_snapshot[key] = "stack" if key == "mode" else str(uuid.uuid4())
    await s.db.commit()
    with pytest.raises(HTTPException) as error:
        await confirm(s, **({"gym_profile_id": None} if change == "no_gym" else {}))
    assert error.value.status_code in (400, 404)
    assert not (await s.db.execute(select(WorkoutPlan).where(
        WorkoutPlan.app_user_id == s.user.id))).scalars().all()


@pytest.mark.asyncio
async def test_legacy_confirmation_keeps_context_nullable(scenario):
    s = scenario
    ex = s.day.exercises[0]
    ex.load_mode = ex.setup_id = ex.load_snapshot = None
    result = await confirm(s, gym_profile_id=None)
    plan = await s.db.get(WorkoutPlan, result.created_plan_ids[0])
    exercise = (await s.db.execute(select(WorkoutPlanExercise).where(
        WorkoutPlanExercise.plan_id == plan.id))).scalar_one()
    assert plan.gym_profile_id is None and plan.gym_snapshot is None
    assert exercise.load_mode is None and exercise.setup_id is None and exercise.load_snapshot is None


@pytest.mark.asyncio
async def test_shared_day_assignments_keep_confirmed_gym(scenario, active_block):
    from tests.integration.test_regeneration_preserves_fact import _day

    s = scenario
    first = await _day(s.db, s.user.id, active_block.id, date.today(), day_tag="Push")
    repeated = await _day(s.db, s.user.id, active_block.id, date.today() + timedelta(days=2),
                          day_tag="Push__2")
    result = await confirm(s)
    await s.db.refresh(first)
    await s.db.refresh(repeated)
    assert first.plan_id == repeated.plan_id == result.created_plan_ids[0]
    split = (await s.db.execute(select(UserSplit).where(UserSplit.app_user_id == s.user.id))).scalar_one()
    assert split.selected_plans["0"] == first.plan_id
    plan = await s.db.get(WorkoutPlan, first.plan_id)
    assert plan.gym_profile_id == s.gym.id
    assert plan.gym_snapshot["name"] == "Studio"

