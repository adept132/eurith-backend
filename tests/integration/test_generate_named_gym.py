import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete

from api.routers import plans
from api.schemas.plan import GeneratePlanRequest, GeneratePlanPreviewRequest, GenerationComparison
from api.services.models import GymProfile, GymExerciseSetup, ExerciseLoadPreference, UserExercisePreference, Exercise
from tests.test_generate_endpoint import _ex


@pytest_asyncio.fixture
async def scenario(db, test_user, monkeypatch):
    gym = GymProfile(id=uuid.uuid4(), app_user_id=test_user.id, name="Studio",
                     equipment=["block_machine"], bars=[], discs=[], steps=[])
    db.add(gym)
    await db.commit()
    await db.refresh(gym)
    machine = _ex(900001, "Грудь")
    machine.equipment_needed = ["block_machine"]
    profile = SimpleNamespace(experience_level="intermediate", settings={}, volume_budget={})
    day = SimpleNamespace(name="Push", template_type="push", muscle_targets=[1])
    blueprint = SimpleNamespace(id=uuid.uuid4(), name="Split", length_days=1,
                                slots=[SimpleNamespace(day_order=1, day=day)])
    pool = [machine]
    monkeypatch.setattr(plans, "_load_generation_context", AsyncMock(return_value=(profile, blueprint, pool, None)))
    monkeypatch.setattr(plans.VolumeService, "calculate_session_targets", AsyncMock(return_value={"chest": {"target_sets": 4}}))
    monkeypatch.setattr(plans, "_generation_comparison", AsyncMock(return_value=GenerationComparison(applied_from=date.today(), mode="full")))
    yield SimpleNamespace(gym=gym, machine=machine, pool=pool, profile=profile, db=db, user=test_user)
    await db.rollback()
    await db.execute(delete(UserExercisePreference).where(UserExercisePreference.app_user_id == test_user.id))
    await db.commit()


async def setup(s, mode="plate_loaded"):
    row = GymExerciseSetup(id=uuid.uuid4(), gym_id=s.gym.id, exercise_source="global",
                           exercise_id=s.machine.id, load_mode=mode, is_available=True,
                           is_preferred=False, step_value=2.5, step_unit="kg", loading_sides=2,
                           weight_basis="plates_only" if mode == "plate_loaded" else "displayed")
    s.db.add(row)
    await s.db.commit()
    await s.db.refresh(row)
    return row


async def generate(s):
    return await plans.generate_plan(GeneratePlanRequest(gym_profile_id=s.gym.id), s.db, s.user)


@pytest.mark.asyncio
async def test_plate_variant_resolves_and_summary_records_gym(scenario):
    s = scenario
    row = await setup(s)
    result = await generate(s)
    ex = result.days[0].exercises[0]
    assert ex.load_mode == "plate_loaded"
    assert ex.setup_id == row.id
    assert ex.load_snapshot["gym_id"] == str(s.gym.id)
    assert ex.load_snapshot["step_value"] == 2.5
    assert result.inputs.gym_profile_id == s.gym.id
    assert result.inputs.gym_name == "Studio"
    assert result.inputs.allowed_equipment == ["block_machine"]
    assert result.inputs.equipment_unrestricted is False


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["setup", "bench", "allowed"])
async def test_unavailable_candidates_excluded_with_actionable_issue(scenario, missing):
    s = scenario
    if missing != "setup":
        await setup(s)
    if missing == "bench":
        s.machine.equipment_needed.append("bench")
    if missing == "allowed":
        s.db.add(ExerciseLoadPreference(app_user_id=s.user.id, exercise_source="global", exercise_id=s.machine.id,
                                       enabled_modes=["stack"], preferred_mode="stack"))
        await s.db.commit()
    result = await generate(s)
    assert result.days[0].exercises == []
    assert any(issue.action.type == "change_equipment" for issue in result.issues)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["setup", "bench"])
async def test_empty_draft_preview_preserves_actionable_gym_issue(scenario, missing):
    s = scenario
    if missing == "bench":
        await setup(s)
        s.machine.equipment_needed.append("bench")
    generated = await generate(s)
    assert generated.days[0].exercises == []
    preview = await plans.preview_generated_plan(
        GeneratePlanPreviewRequest(days=generated.days, gym_profile_id=s.gym.id), s.db, s.user)
    issues = [issue for issue in preview.issues if issue.muscle == "chest"]
    assert len(issues) == 1
    assert issues[0].code == "gym_equipment_unavailable"
    assert issues[0].action.type == "change_equipment"
    assert ("no_machine_setup" if missing == "setup" else "missing_equipment") in issues[0].reason


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["foreign", "deleted"])
async def test_rejects_unowned_or_deleted_gym(scenario, state):
    s = scenario
    if state == "deleted":
        s.gym.deleted_at = datetime.now(timezone.utc)
        await s.db.commit()
    else:
        s.user = SimpleNamespace(id=-1)
    with pytest.raises(HTTPException) as error:
        await generate(s)
    assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["setup_id", "load_mode", "deleted_setup", "snapshot_gym", "snapshot_setup", "snapshot_mode"])
async def test_preview_rejects_tampered_or_stale_context(scenario, tamper):
    s = scenario
    row = await setup(s)
    generated = await generate(s)
    data = generated.model_dump(mode="json")["days"]
    if tamper == "deleted_setup":
        row.deleted_at = datetime.now(timezone.utc)
        await s.db.commit()
    elif tamper.startswith("snapshot_"):
        key = {"snapshot_gym": "gym_id", "snapshot_setup": "setup_id", "snapshot_mode": "mode"}[tamper]
        data[0]["exercises"][0]["load_snapshot"][key] = "stack" if key == "mode" else str(uuid.uuid4())
    else:
        data[0]["exercises"][0][tamper] = str(uuid.uuid4()) if tamper == "setup_id" else "stack"
    with pytest.raises(HTTPException) as error:
        await plans.preview_generated_plan(GeneratePlanPreviewRequest(days=data, gym_profile_id=s.gym.id), s.db, s.user)
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_preview_refreshes_snapshot_from_current_setup(scenario):
    s = scenario
    row = await setup(s)
    generated = await generate(s)
    row.step_value = 5
    await s.db.commit()
    result = await plans.preview_generated_plan(GeneratePlanPreviewRequest(days=generated.days, gym_profile_id=s.gym.id), s.db, s.user)
    assert result.days[0].exercises[0].load_snapshot["step_value"] == 5
    assert generated.days[0].exercises[0].load_snapshot["step_value"] == 2.5


@pytest.mark.asyncio
async def test_preview_rejects_context_when_gym_removed(scenario):
    s = scenario
    await setup(s)
    generated = await generate(s)
    with pytest.raises(HTTPException):
        await plans.preview_generated_plan(GeneratePlanPreviewRequest(days=generated.days), s.db, s.user)


async def preference(s, value):
    row = Exercise(name=f"Preference test {uuid.uuid4()}", category="strength", main_muscle_group="chest",
                   secondary_muscle_groups=[], equipment_needed=[], difficulty="beginner",
                   source="test", app_user_id=s.user.id)
    s.db.add(row)
    await s.db.flush()
    s.machine.id = row.id
    s.db.add(UserExercisePreference(app_user_id=s.user.id, exercise_id=row.id,
                                   exercise_name=row.name, preference=value))
    await s.db.commit()


@pytest.mark.asyncio
async def test_preview_rejects_newly_disliked_exercise(scenario):
    s = scenario
    await preference(s, "disliked")
    await setup(s)
    from api.schemas.plan import GeneratedDayOut, GeneratedExerciseOut
    day = GeneratedDayOut(day_tag="Push", day_name="Push", coverage={}, exercises=[
        GeneratedExerciseOut(exercise_id=s.machine.id, name="Exercise", target_sets=3,
                             order_index=0, fatigue_tier=2, primary_muscle="Грудь")])
    with pytest.raises(HTTPException):
        await plans.preview_generated_plan(GeneratePlanPreviewRequest(days=[day], gym_profile_id=s.gym.id), s.db, s.user)


@pytest.mark.asyncio
@pytest.mark.parametrize("value, available, count", [("favorite", False, 0), ("favorite", True, 1), ("disliked", True, 0)])
async def test_equipment_and_preferences_both_apply(scenario, value, available, count):
    s = scenario
    await preference(s, value)
    if available:
        await setup(s)
    generated = await generate(s)
    assert len(generated.days[0].exercises) == count
    if count:
        assert generated.days[0].exercises[0].preference == "favorite"


@pytest.mark.asyncio
async def test_both_machine_categories_are_alternatives(scenario):
    s = scenario
    s.machine.equipment_needed = ["block_machine", "free_machine"]
    await setup(s)
    result = await generate(s)
    assert result.days[0].exercises[0].load_mode == "plate_loaded"


@pytest.mark.asyncio
async def test_saved_explicit_replacement_cannot_insert_unavailable_machine(scenario):
    s = scenario
    await setup(s)
    other = _ex(900002, "Грудь")
    other.equipment_needed = ["block_machine"]
    s.pool.append(other)
    s.profile.settings = {"generator_rules": [{"scope": "all", "command": {
        "type": "REPLACE_EXERCISE", "params": {"from_exercise_id": s.machine.id,
        "to": {"exercise_id": other.id, "name": other.name}}}}]}
    result = await generate(s)
    assert [ex.exercise_id for ex in result.days[0].exercises] == [s.machine.id]
    assert result.days[0].warnings


@pytest.mark.asyncio
async def test_saved_replacement_refreshes_favorite_marker(scenario):
    s = scenario
    await preference(s, "favorite")
    await setup(s)
    other = _ex(900002, "Грудь")
    other.equipment_needed = ["bodyweight"]
    s.pool.append(other)
    s.profile.settings = {"generator_rules": [{"scope": "all", "command": {
        "type": "REPLACE_EXERCISE", "params": {"from_exercise_id": s.machine.id,
        "to": {"exercise_id": other.id, "name": other.name}}}}]}
    result = await generate(s)
    assert result.days[0].exercises[0].exercise_id == other.id
    assert result.days[0].exercises[0].preference is None
