from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytest_plugins = ["tests.integration.conftest"]

from api.schemas.workout_history import WorkoutHistoryDraft
from api.services.models import (
    AppUser,
    Exercise,
    PeriodReport,
    UserExerciseProgressionState,
    WorkoutRecalculationOperation,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.workout_history import replace_history_workout
from api.services.workout_recalculation import (
    recalculate_workout_revision,
    run_pending_recalculations,
)


T0 = datetime(2026, 8, 19, 18, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


@pytest_asyncio.fixture
async def recalc_exercises(db: AsyncSession, test_user: AppUser) -> tuple[Exercise, Exercise]:
    marker = uuid.uuid4().hex[:8]
    exercises = (
        Exercise(
            name=f"Recalc old {marker}",
            category="base",
            main_muscle_group="chest",
            difficulty="beginner",
            equipment_needed=[],
            source="custom",
            app_user_id=test_user.id,
        ),
        Exercise(
            name=f"Recalc new {marker}",
            category="base",
            main_muscle_group="back",
            difficulty="beginner",
            equipment_needed=[],
            source="custom",
            app_user_id=test_user.id,
        ),
    )
    db.add_all(exercises)
    await db.commit()
    for exercise in exercises:
        await db.refresh(exercise)
    return exercises


@pytest_asyncio.fixture
async def edited_workout(
    db: AsyncSession,
    test_user: AppUser,
    recalc_exercises: tuple[Exercise, Exercise],
) -> WorkoutSession:
    exercise = recalc_exercises[0]
    workout = WorkoutSession(
        app_user_id=test_user.id,
        client_uuid=f"recalc-{uuid.uuid4().hex}",
        source="free",
        status="finished",
        entry_mode="manual",
        revision=2,
        started_at=T0,
        finished_at=T1,
    )
    db.add(workout)
    await db.flush()
    session_exercise = WorkoutSessionExercise(
        workout_session_id=workout.id,
        client_uuid="recalc-exercise",
        exercise_id=exercise.id,
        order_index=0,
    )
    db.add(session_exercise)
    await db.flush()
    db.add(
        WorkoutSessionSet(
            workout_session_exercise_id=session_exercise.id,
            client_uuid="recalc-set",
            set_number=1,
            set_type="normal",
            weight=100,
            reps=5,
            effort_level="medium",
            is_completed=True,
        )
    )
    db.add(
        WorkoutRecalculationOperation(
            workout_id=workout.id,
            revision=2,
            status="pending",
            exercise_ids=[exercise.id],
        )
    )
    await db.commit()
    await db.refresh(workout)
    return workout


@pytest.mark.asyncio
async def test_recalculation_is_idempotent(db: AsyncSession, edited_workout):
    first = await recalculate_workout_revision(db, edited_workout.id, 2)
    second = await recalculate_workout_revision(db, edited_workout.id, 2)

    assert first.status == "completed"
    assert second.status == "already_completed"


@pytest.mark.asyncio
async def test_recalculation_rebuilds_progression_and_records(
    db: AsyncSession,
    test_user: AppUser,
    edited_workout: WorkoutSession,
    recalc_exercises: tuple[Exercise, Exercise],
):
    await recalculate_workout_revision(db, edited_workout.id, 2)

    state = (
        await db.execute(
            select(UserExerciseProgressionState).where(
                UserExerciseProgressionState.app_user_id == test_user.id,
                UserExerciseProgressionState.exercise_id == recalc_exercises[0].id,
            )
        )
    ).scalar_one()
    assert state.working_e1rm is not None
    assert state.records["weight_at_reps"]["5"]["weight"] == 100.0


@pytest.mark.asyncio
async def test_closed_period_report_is_unchanged(
    db: AsyncSession,
    test_user: AppUser,
    edited_workout: WorkoutSession,
):
    report = PeriodReport(
        app_user_id=test_user.id,
        period_type="week",
        period_start=date(2026, 8, 10),
        period_end=date(2026, 8, 16),
        payload={"metrics": {"volume": 1234}, "closed": True},
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    before = dict(report.payload)

    await recalculate_workout_revision(db, edited_workout.id, 2)
    await db.refresh(report)

    assert report.payload == before


@pytest.mark.asyncio
async def test_history_replace_enqueues_union_of_old_and_new_exercise_ids(
    db: AsyncSession,
    test_user: AppUser,
    edited_workout: WorkoutSession,
    recalc_exercises: tuple[Exercise, Exercise],
):
    old_exercise, new_exercise = recalc_exercises
    draft = WorkoutHistoryDraft(
        client_uuid="recalc-history-workout",
        base_revision=2,
        entry_mode="manual",
        started_at=T0,
        finished_at=T1,
        notes=None,
        session_rpe=None,
        exercises=[
            {
                "client_uuid": "recalc-new-exercise",
                "exercise_id": new_exercise.id,
                "order_index": 0,
                "sets": [],
            }
        ],
    )

    await replace_history_workout(db, test_user.id, edited_workout.id, draft)
    operation = (
        await db.execute(
            select(WorkoutRecalculationOperation).where(
                WorkoutRecalculationOperation.workout_id == edited_workout.id,
                WorkoutRecalculationOperation.revision == 3,
            )
        )
    ).scalar_one()

    assert set(operation.exercise_ids) == {old_exercise.id, new_exercise.id}


@pytest.mark.asyncio
async def test_pending_recalculation_sweeper_completes_durable_operation(
    db: AsyncSession, edited_workout: WorkoutSession
):
    processed = await run_pending_recalculations(limit=10)

    operation = (
        await db.execute(
            select(WorkoutRecalculationOperation).where(
                WorkoutRecalculationOperation.workout_id == edited_workout.id,
                WorkoutRecalculationOperation.revision == 2,
            )
        )
    ).scalar_one()
    await db.refresh(operation)
    assert processed >= 1
    assert operation.status == "completed"


@pytest.mark.asyncio
async def test_failed_sweeper_operation_is_durable_and_retryable(
    db: AsyncSession,
    edited_workout: WorkoutSession,
    recalc_exercises: tuple[Exercise, Exercise],
):
    operation = (
        await db.execute(
            select(WorkoutRecalculationOperation).where(
                WorkoutRecalculationOperation.workout_id == edited_workout.id,
                WorkoutRecalculationOperation.revision == 2,
            )
        )
    ).scalar_one()
    operation.exercise_ids = [2_147_483_000]
    await db.commit()

    await run_pending_recalculations(limit=10)
    await db.refresh(operation)
    assert operation.status == "failed"
    assert operation.attempts == 1
    assert operation.last_error
    assert operation.next_attempt_at is not None

    operation.exercise_ids = [recalc_exercises[0].id]
    operation.next_attempt_at = T0
    await db.commit()

    await run_pending_recalculations(limit=10)
    await db.refresh(operation)
    assert operation.status == "completed"
    assert operation.attempts == 2
