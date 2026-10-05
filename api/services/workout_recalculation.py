from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUserProfile,
    UserExerciseProgressionState,
    WorkoutRecalculationOperation,
    WorkoutSession,
    WorkoutSessionExercise,
)
from api.services.progression import repository as progression_repository
from api.services.progression.engine import plan_exercise
from api.services.progression.records_repository import rebuild_records
from api.services.progression.resolve import override_for
from app.database import SessionLocal


RecalculationResultStatus = Literal["completed", "already_completed"]


@dataclass(frozen=True)
class RecalculationResult:
    status: RecalculationResultStatus
    workout_id: int
    revision: int
    exercise_ids: tuple[int, ...]


async def _load_operation(
    db: AsyncSession,
    workout_id: int,
    revision: int,
) -> WorkoutRecalculationOperation | None:
    return (
        await db.execute(
            select(WorkoutRecalculationOperation)
            .where(
                WorkoutRecalculationOperation.workout_id == workout_id,
                WorkoutRecalculationOperation.revision == revision,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _latest_session_exercise(
    db: AsyncSession,
    app_user_id: int,
    exercise_id: int,
) -> WorkoutSessionExercise | None:
    return (
        await db.execute(
            select(WorkoutSessionExercise)
            .join(WorkoutSession)
            .where(
                WorkoutSession.app_user_id == app_user_id,
                WorkoutSession.status == "finished",
                WorkoutSession.finished_at.is_not(None),
                WorkoutSessionExercise.exercise_id == exercise_id,
            )
            .options(
                selectinload(WorkoutSessionExercise.exercise),
                selectinload(WorkoutSessionExercise.sets),
                selectinload(WorkoutSessionExercise.workout_session),
            )
            .order_by(WorkoutSession.finished_at.desc(), WorkoutSession.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _refresh_progression(
    db: AsyncSession,
    app_user_id: int,
    exercise_ids: tuple[int, ...],
) -> None:
    profile = (
        await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
        )
    ).scalar_one_or_none()
    experience_level = profile.experience_level if profile else None
    settings = profile.settings if profile else None

    for exercise_id in exercise_ids:
        session_exercise = await _latest_session_exercise(
            db, app_user_id, exercise_id
        )
        if session_exercise is None:
            await progression_repository.refresh_state(
                db, app_user_id, exercise_id, None
            )
            state = (
                await db.execute(
                    select(UserExerciseProgressionState).where(
                        UserExerciseProgressionState.app_user_id == app_user_id,
                        UserExerciseProgressionState.exercise_id == exercise_id,
                    )
                )
            ).scalar_one()
            state.next_prescription = None
            continue

        workout = session_exercise.workout_session
        phase_effort_tier = (
            await progression_repository.resolve_phase_effort_tier(
                db,
                workout.app_user_mesocycle_id,
                workout.mesocycle_phase,
                training_block_id=workout.training_block_id,
            )
        )
        context = await progression_repository.build_context(
            db,
            session_exercise,
            app_user_id,
            experience_level,
            settings,
            phase_effort_tier=phase_effort_tier,
        )
        next_prescription = plan_exercise(
            context,
            override=override_for(settings, exercise_id),
            provisional=True,
        )
        await progression_repository.refresh_state(
            db, app_user_id, exercise_id, next_prescription
        )


async def _mark_failed(
    db: AsyncSession,
    workout_id: int,
    revision: int,
    error: Exception,
) -> None:
    operation = await _load_operation(db, workout_id, revision)
    if operation is None:
        return
    operation.status = "failed"
    operation.attempts += 1
    operation.last_error = str(error)[:4000]
    delay_seconds = min(30 * (2 ** max(operation.attempts - 1, 0)), 3600)
    operation.next_attempt_at = datetime.now(timezone.utc) + timedelta(
        seconds=delay_seconds
    )
    await db.commit()


async def recalculate_workout_revision(
    db: AsyncSession,
    workout_id: int,
    revision: int,
) -> RecalculationResult:
    """Rebuild mutable projections without invoking the report materializer."""

    operation = await _load_operation(db, workout_id, revision)
    workout = await db.get(WorkoutSession, workout_id)
    if workout is None:
        await db.rollback()
        raise LookupError("workout not found")

    if operation is None:
        current_ids = list(
            (
                await db.execute(
                    select(WorkoutSessionExercise.exercise_id).where(
                        WorkoutSessionExercise.workout_session_id == workout_id
                    )
                )
            ).scalars()
        )
        operation = WorkoutRecalculationOperation(
            workout_id=workout_id,
            revision=revision,
            status="pending",
            exercise_ids=sorted(set(current_ids)),
        )
        db.add(operation)
        await db.flush()

    exercise_ids = tuple(sorted({int(item) for item in operation.exercise_ids}))
    if operation.status == "completed":
        result = RecalculationResult(
            status="already_completed",
            workout_id=workout_id,
            revision=revision,
            exercise_ids=exercise_ids,
        )
        await db.rollback()
        return result

    operation.status = "processing"
    operation.last_error = None
    operation.next_attempt_at = None
    try:
        await _refresh_progression(db, workout.app_user_id, exercise_ids)
        await rebuild_records(db, workout.app_user_id, exercise_ids)
        # Current volume/adherence/goal views query raw facts and the two
        # projections above on read. Closed PeriodReport rows are deliberately
        # absent from this orchestration and therefore remain immutable.
        operation.status = "completed"
        operation.attempts += 1
        operation.completed_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        await _mark_failed(db, workout_id, revision, exc)
        raise

    return RecalculationResult(
        status="completed",
        workout_id=workout_id,
        revision=revision,
        exercise_ids=exercise_ids,
    )


async def run_pending_recalculations(limit: int = 20) -> int:
    """Process a bounded durable batch, including retryable/stale operations."""

    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=5)
    async with SessionLocal() as db:
        keys = list(
            (
                await db.execute(
                    select(
                        WorkoutRecalculationOperation.workout_id,
                        WorkoutRecalculationOperation.revision,
                    )
                    .where(
                        or_(
                            WorkoutRecalculationOperation.status == "pending",
                            and_(
                                WorkoutRecalculationOperation.status == "failed",
                                or_(
                                    WorkoutRecalculationOperation.next_attempt_at.is_(None),
                                    WorkoutRecalculationOperation.next_attempt_at <= now,
                                ),
                            ),
                            and_(
                                WorkoutRecalculationOperation.status == "processing",
                                WorkoutRecalculationOperation.updated_at < stale_before,
                            ),
                        )
                    )
                    .order_by(
                        WorkoutRecalculationOperation.created_at,
                        WorkoutRecalculationOperation.id,
                    )
                    .limit(limit)
                )
            ).all()
        )

    processed = 0
    for workout_id, revision in keys:
        try:
            async with SessionLocal() as db:
                await recalculate_workout_revision(db, workout_id, revision)
        except Exception as exc:  # noqa: BLE001 - durable row records the failure
            print(
                "[workout-recalculation] operation failed "
                f"workout={workout_id} revision={revision}: {exc}"
            )
        processed += 1
    return processed


async def workout_recalculation_worker(
    stop: asyncio.Event,
    interval_seconds: int = 10,
) -> None:
    """Restart-safe sweeper for durable workout history recalculations."""

    while not stop.is_set():
        try:
            await run_pending_recalculations()
        except Exception as exc:  # noqa: BLE001 - one sweep must not kill the worker
            print(f"[workout-recalculation] worker iteration failed: {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
