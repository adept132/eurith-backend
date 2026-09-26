from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUser,
    Exercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)


T0 = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


@pytest_asyncio.fixture
async def history_exercise(db: AsyncSession, test_user: AppUser) -> Exercise:
    exercise = Exercise(
        name=f"History exercise {uuid.uuid4().hex[:8]}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(exercise)
    await db.commit()
    await db.refresh(exercise)
    return exercise


def history_body(
    exercise_id: int,
    *,
    base_revision: int = 0,
    entry_mode: str = "manual",
    weight: str = "80.00",
) -> dict:
    return {
        "client_uuid": "history-workout-1",
        "base_revision": base_revision,
        "entry_mode": entry_mode,
        "started_at": T0.isoformat(),
        "finished_at": T1.isoformat(),
        "notes": "Session note",
        "session_rpe": 7.5,
        "exercises": [
            {
                "client_uuid": "history-exercise-1",
                "exercise_id": exercise_id,
                "order_index": 0,
                "superset_group": None,
                "notes": "Exercise note",
                "sets": [
                    {
                        "client_uuid": "history-set-1",
                        "set_number": 1,
                        "set_type": "normal",
                        "weight": weight,
                        "reps": 8,
                        "effort_level": "medium",
                        "notes": "Set note",
                        "is_completed": True,
                    }
                ],
            }
        ],
    }


async def _create_finished_workout(
    db: AsyncSession,
    user_id: int,
    exercise_id: int,
) -> WorkoutSession:
    workout = WorkoutSession(
        app_user_id=user_id,
        client_uuid=f"workout-{uuid.uuid4().hex}",
        source="plan",
        status="finished",
        entry_mode="live",
        revision=1,
        started_at=T0,
        finished_at=T1,
    )
    db.add(workout)
    await db.flush()
    session_exercise = WorkoutSessionExercise(
        workout_session_id=workout.id,
        client_uuid="history-exercise-1",
        exercise_id=exercise_id,
        order_index=0,
        recommended_rir=2,
        recommended_weight=80,
        recommended_rep_min=6,
        recommended_rep_max=10,
        target_sets=3,
        prescription={"target_reps": 8},
        live_prescription={"target_reps": 9},
    )
    db.add(session_exercise)
    await db.flush()
    db.add(
        WorkoutSessionSet(
            workout_session_exercise_id=session_exercise.id,
            client_uuid="history-set-1",
            set_number=1,
            set_type="normal",
            weight=80,
            reps=8,
            effort_level="medium",
            is_completed=True,
            is_max_reps=True,
        )
    )
    await db.commit()
    await db.refresh(workout)
    return workout


@pytest.mark.asyncio
async def test_manual_create_is_finished(client, history_exercise: Exercise):
    response = await client.post(
        "/workouts/history", json=history_body(history_exercise.id)
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["workout"]["status"] == "finished"
    assert payload["workout"]["entry_mode"] == "manual"
    assert payload["workout"]["revision"] == 0
    assert payload["recalculation_status"] == "pending"


@pytest.mark.asyncio
async def test_manual_create_replay_is_idempotent(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    body = history_body(history_exercise.id)

    first = await client.post("/workouts/history", json=body)
    second = await client.post("/workouts/history", json=body)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["workout"]["id"] == first.json()["workout"]["id"]
    rows = list(
        (
            await db.execute(
                select(WorkoutSession).where(
                    WorkoutSession.app_user_id == test_user.id,
                    WorkoutSession.client_uuid == body["client_uuid"],
                )
            )
        ).scalars()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_manual_create_changed_replay_returns_typed_conflict(
    client, history_exercise: Exercise
):
    body = history_body(history_exercise.id)
    first = await client.post("/workouts/history", json=body)
    changed = {**body, "notes": "changed after a lost response"}

    second = await client.post("/workouts/history", json=changed)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["error"] == "history_idempotency_conflict"
    assert detail["server_draft"]["notes"] == "Session note"


@pytest.mark.asyncio
async def test_manual_create_rejects_client_uuid_owned_by_live_workout(
    client,
    db: AsyncSession,
    test_user: AppUser,
    history_exercise: Exercise,
):
    live = await _create_finished_workout(db, test_user.id, history_exercise.id)
    body = history_body(history_exercise.id)
    body["client_uuid"] = live.client_uuid

    response = await client.post("/workouts/history", json=body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "history_idempotency_conflict"


@pytest.mark.asyncio
async def test_history_list_returns_completed_owned_workouts(
    client, history_exercise: Exercise
):
    created = await client.post(
        "/workouts/history", json=history_body(history_exercise.id)
    )
    workout_id = created.json()["workout"]["id"]

    response = await client.get("/workouts/history")

    assert response.status_code == 200, response.text
    items = response.json()["workouts"]
    assert len(items) == 1
    assert items[0] | {
        "id": workout_id,
        "entry_mode": "manual",
        "revision": 0,
        "exercises_count": 1,
        "sets_count": 1,
    } == items[0]


@pytest.mark.asyncio
async def test_patch_rejects_stale_revision_with_server_draft(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    body = history_body(
        history_exercise.id, base_revision=workout.revision - 1, entry_mode="live"
    )

    response = await client.patch(f"/workouts/{workout.id}/history", json=body)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "history_revision_conflict"
    assert detail["server_revision"] == 1
    assert detail["server_draft"]["base_revision"] == 1


@pytest.mark.asyncio
async def test_patch_preserves_immutable_prescription_entry_mode_and_hidden_set_fact(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    body = history_body(
        history_exercise.id,
        base_revision=workout.revision,
        entry_mode="live",
        weight="82.50",
    )

    response = await client.patch(f"/workouts/{workout.id}/history", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["workout"]["status"] == "finished"
    assert response.json()["workout"]["revision"] == 2

    refreshed = (
        await db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == workout.id)
            .options(
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.sets
                )
            )
        )
    ).scalar_one()
    exercise = refreshed.exercises[0]
    assert refreshed.entry_mode == "live"
    assert exercise.prescription == {"target_reps": 8}
    assert exercise.live_prescription == {"target_reps": 9}
    assert exercise.recommended_rir == 2
    assert exercise.sets[0].is_max_reps is True
    assert float(exercise.sets[0].weight) == 82.5


@pytest.mark.asyncio
async def test_patch_can_reorder_exercises_and_sets_with_unique_positions(
    client, history_exercise: Exercise
):
    body = history_body(history_exercise.id)
    body["exercises"][0]["sets"].append(
        {
            **body["exercises"][0]["sets"][0],
            "client_uuid": "history-set-2",
            "set_number": 2,
        }
    )
    body["exercises"].append(
        {
            **body["exercises"][0],
            "client_uuid": "history-exercise-2",
            "order_index": 1,
            "sets": [],
        }
    )
    created = await client.post("/workouts/history", json=body)
    assert created.status_code == 201, created.text
    workout_id = created.json()["workout"]["id"]

    changed = {**body, "base_revision": 0}
    changed["exercises"] = [
        {**body["exercises"][0], "order_index": 1,
         "sets": [
             {**body["exercises"][0]["sets"][0], "set_number": 2},
             {**body["exercises"][0]["sets"][1], "set_number": 1},
         ]},
        {**body["exercises"][1], "order_index": 0},
    ]
    response = await client.patch(f"/workouts/{workout_id}/history", json=changed)

    assert response.status_code == 200, response.text
    exercises = response.json()["workout"]["exercises"]
    assert [(item["client_uuid"], item["order_index"]) for item in exercises] == [
        ("history-exercise-2", 0), ("history-exercise-1", 1)
    ]
    assert [(item["client_uuid"], item["set_number"]) for item in exercises[1]["sets"]] == [
        ("history-set-2", 1), ("history-set-1", 2)
    ]


@pytest.mark.asyncio
async def test_patch_legacy_null_uuids_preserves_immutable_and_hidden_fields(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    loaded = (
        await db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == workout.id)
            .options(selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.sets))
        )
    ).scalar_one()
    original_exercise = loaded.exercises[0]
    original_set = original_exercise.sets[0]
    original_exercise.client_uuid = None
    original_set.client_uuid = None
    original_set.superset_round = 1
    original_set.is_anomalous = True
    await db.commit()

    body = history_body(
        history_exercise.id,
        base_revision=workout.revision,
        entry_mode="live",
        weight="82.50",
    )
    body["exercises"][0]["client_uuid"] = f"server-exercise-{original_exercise.id}"
    body["exercises"][0]["sets"][0]["client_uuid"] = f"server-set-{original_set.id}"

    response = await client.patch(f"/workouts/{workout.id}/history", json=body)

    assert response.status_code == 200, response.text
    refreshed = (
        await db.execute(
            select(WorkoutSessionExercise)
            .where(WorkoutSessionExercise.workout_session_id == workout.id)
            .options(selectinload(WorkoutSessionExercise.sets))
        )
    ).scalar_one()
    assert refreshed.id == original_exercise.id
    assert refreshed.prescription == {"target_reps": 8}
    assert refreshed.live_prescription == {"target_reps": 9}
    assert refreshed.sets[0].id == original_set.id
    assert refreshed.sets[0].is_max_reps is True
    assert refreshed.sets[0].superset_round == 1


@pytest.mark.asyncio
async def test_manual_history_runs_anomaly_guard(
    client, db: AsyncSession, history_exercise: Exercise
):
    response = await client.post(
        "/workouts/history",
        json=history_body(history_exercise.id, weight="700.00"),
    )

    assert response.status_code == 201, response.text
    workout_id = response.json()["workout"]["id"]
    workout_set = (
        await db.execute(
            select(WorkoutSessionSet)
            .join(WorkoutSessionExercise)
            .where(WorkoutSessionExercise.workout_session_id == workout_id)
        )
    ).scalar_one()
    assert workout_set.is_anomalous is True


@pytest.mark.asyncio
async def test_patch_rejects_entry_mode_change(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)

    response = await client.patch(
        f"/workouts/{workout.id}/history",
        json=history_body(
            history_exercise.id,
            base_revision=workout.revision,
            entry_mode="manual",
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "immutable_history_field"


@pytest.mark.asyncio
async def test_patch_invalid_exercise_is_atomic(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    body = history_body(
        2_147_483_000, base_revision=workout.revision, entry_mode="live"
    )

    response = await client.patch(f"/workouts/{workout.id}/history", json=body)

    assert response.status_code == 422, response.text
    await db.refresh(workout)
    assert workout.revision == 1
    assert workout.edited_at is None


@pytest.mark.asyncio
async def test_patch_hides_foreign_workout(
    client, db: AsyncSession, history_exercise: Exercise
):
    other = AppUser(
        firebase_uid=f"foreign-{uuid.uuid4().hex}",
        email=f"foreign-{uuid.uuid4().hex}@example.com",
        display_name="Foreign",
    )
    db.add(other)
    await db.flush()
    workout = WorkoutSession(
        app_user_id=other.id,
        source="free",
        status="finished",
        entry_mode="live",
        revision=1,
        started_at=T0,
        finished_at=T1,
    )
    db.add(workout)
    await db.commit()
    await db.refresh(workout)

    response = await client.patch(
        f"/workouts/{workout.id}/history",
        json=history_body(
            history_exercise.id, base_revision=workout.revision, entry_mode="live"
        ),
    )

    assert response.status_code == 404
    await db.delete(workout)
    await db.delete(other)
    await db.commit()
