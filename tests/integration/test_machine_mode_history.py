"""Mixed machine variants retain one exercise timeline and separate weight records."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from api.services.models import UserExerciseProgressionState, WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet
from api.services.progression.records_repository import rebuild_records
from api.services.progression.records_repository import load_variant_key


@pytest.mark.asyncio
async def test_mixed_history_keeps_saved_labels_and_separate_records(client, db, test_user, seeded_history):
    gym_id, stack_id, plate_id = uuid4(), uuid4(), uuid4()
    workout = WorkoutSession(app_user_id=test_user.id, source="free", status="finished",
        finished_at=datetime.now(timezone.utc), gym_profile_id=gym_id,
        gym_snapshot={"id": str(gym_id), "name": "Старый зал"})
    db.add(workout)
    await db.flush()
    movement = WorkoutSessionExercise(workout_session_id=workout.id,
        exercise_id=seeded_history.id, order_index=0)
    db.add(movement)
    await db.flush()
    stack = WorkoutSessionSet(workout_session_exercise_id=movement.id, set_number=1,
        set_type="normal", weight=100, reps=5, is_completed=True, load_mode="stack",
        gym_profile_id=gym_id, setup_id=stack_id,
        load_snapshot={"weight_basis": "displayed", "mode": "stack"})
    plate = WorkoutSessionSet(workout_session_exercise_id=movement.id, set_number=2,
        set_type="normal", weight=30, reps=8, is_completed=True, load_mode="plate_loaded",
        gym_profile_id=gym_id, setup_id=plate_id,
        load_snapshot={"weight_basis": "plates_only", "mode": "plate_loaded"})
    unknown = WorkoutSessionSet(workout_session_exercise_id=movement.id, set_number=3,
        set_type="normal", weight=120, reps=6, is_completed=True)
    db.add_all([stack, plate, unknown])
    await db.commit()

    url = f"/exercises/{seeded_history.id}/history"
    listing = await client.get(url)
    assert listing.status_code == 200, listing.text
    point = next(item for item in listing.json() if item["workout_id"] == workout.id)
    assert (point["sets_count"], point["total_reps"]) == (3, 19)
    detail = await client.get(f"{url}/{workout.id}")
    assert detail.status_code == 200, detail.text
    sets = detail.json()["sets"]
    assert [item["load_mode"] for item in sets] == ["stack", "plate_loaded", None]
    assert [item["gym_name"] for item in sets] == ["Старый зал", "Старый зал", "Старый зал"]
    assert sets[1]["weight_basis"] == "plates_only"
    assert sets[0]["setup_id"] == str(stack_id)
    plate_last = await client.get(f"/exercises/{seeded_history.id}/last-performance",
        params={"load_mode": "plate_loaded", "gym_profile_id": str(gym_id),
                "setup_id": str(plate_id), "weight_basis": "plates_only"})
    assert plate_last.status_code == 200, plate_last.text
    assert [item["id"] for item in plate_last.json()["sets"]] == [plate.id]
    achievements = await client.get("/progress/achievements")
    assert achievements.status_code == 200, achievements.text
    current = [item for item in achievements.json() if item["workout_id"] == workout.id
               and item["exercise_id"] == seeded_history.id]
    assert {item["load_mode"] for item in current} == {"stack", "plate_loaded"}
    assert {item["gym_name"] for item in current} == {"Старый зал"}

    await rebuild_records(db, test_user.id, [seeded_history.id])
    await db.commit()
    state = (await db.execute(select(UserExerciseProgressionState).where(
        UserExerciseProgressionState.app_user_id == test_user.id,
        UserExerciseProgressionState.exercise_id == seeded_history.id))).scalar_one()
    variants = state.records["variants"]
    stack_records = variants[f"setup:{stack_id}|basis:displayed"]
    plate_records = variants[f"setup:{plate_id}|basis:plates_only"]
    assert stack_records["weight_at_reps"]["5"]["weight"] == 100
    assert plate_records["weight_at_reps"]["8"]["weight"] == 30
    assert "6" not in stack_records["weight_at_reps"]
    assert "6" not in plate_records["weight_at_reps"]

    unknown.load_mode = "plate_loaded"
    unknown.gym_profile_id = gym_id
    unknown.setup_id = plate_id
    unknown.load_snapshot = {"gym_name": "Старый зал", "weight_basis": "plates_only"}
    await db.commit()
    await rebuild_records(db, test_user.id, [seeded_history.id])
    await db.commit()
    assert float(unknown.weight) == 120
    assert float(stack.weight) == 100
    assert float(plate.weight) == 30
    detail_after = await client.get(f"{url}/{workout.id}")
    assert [item["load_mode"] for item in detail_after.json()["sets"]] == [
        "stack", "plate_loaded", "plate_loaded"]
    await db.refresh(state)
    assert state.records["variants"][f"setup:{plate_id}|basis:plates_only"]["weight_at_reps"]["6"]["weight"] == 120


def test_no_setup_context_key_separates_gym_mode_and_recorded_basis():
    gym_id = uuid4()
    assert load_variant_key("plate_loaded", gym_id, None, {"weight_basis": "plates_only"}) == (
        f"gym:{gym_id}|mode:plate_loaded|basis:plates_only")
    assert load_variant_key("plate_loaded", gym_id, None, {"weight_basis": "including_start_weight"}) != (
        f"gym:{gym_id}|mode:plate_loaded|basis:plates_only")
    assert load_variant_key(None, gym_id, None, {"weight_basis": "plates_only"}) is None
