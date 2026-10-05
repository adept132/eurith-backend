from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    AppUserProfile,
    WorkoutRoutine,
    WorkoutRoutineExercise,
    WorkoutSession,
    WorkoutSessionExercise,
)
from api.services.progression import params as progression_params
from api.services.progression import repository as progression_repo
from api.services.progression.engine import plan_exercise
from api.services.progression.resolve import override_for
from api.services.readiness import repository as readiness_repo
from api.services.active_workout_guard import (
    ActiveWorkoutExists,
    ensure_no_other_active_workout,
)


class RoutineNotFound(Exception):
    pass


def _workout_options():
    return selectinload(WorkoutSession.exercises).options(
        selectinload(WorkoutSessionExercise.exercise),
        selectinload(WorkoutSessionExercise.sets),
    )


async def _load_routine(
    db: AsyncSession, user_id: int, routine_id: int
) -> WorkoutRoutine:
    routine = (
        await db.execute(
            select(WorkoutRoutine)
            .where(
                WorkoutRoutine.id == routine_id,
                WorkoutRoutine.app_user_id == user_id,
            )
            .options(
                selectinload(WorkoutRoutine.exercises).selectinload(
                    WorkoutRoutineExercise.exercise
                )
            )
        )
    ).scalar_one_or_none()
    if routine is None:
        raise RoutineNotFound
    return routine


async def _free_session_context(
    db: AsyncSession, user_id: int, now: datetime
) -> tuple[int | None, int | None, int | None, int | None]:
    active_meso = (
        await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == user_id,
                AppUserMesocycle.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    active_micro = (
        await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == user_id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()

    meso_id = active_meso.id if active_meso else None
    current_phase = active_meso.current_phase if active_meso else None
    micro_id = active_micro.id if active_micro else None

    # Keep the same source of truth as a free start in workout_center.py:
    # the materialized block snapshot wins over AppUserMesocycle.current_phase.
    from api.services.periodization.repository import ensure_active_block

    active_block = await ensure_active_block(db, user_id, now.date())
    training_block_id = active_block.id if active_block else None
    if active_block is not None:
        from api.services.periodization.service import block_coordinate

        coordinate = await block_coordinate(db, user_id, active_block, now.date())
        current_phase = coordinate.phase_number

    return meso_id, current_phase, micro_id, training_block_id


async def start_routine(
    db: AsyncSession,
    user: AppUser,
    routine_id: int,
    now: datetime,
) -> WorkoutSession:
    # Serialize all start paths that opt into this guard. The second routine
    # start observes the first committed active session and returns 409.
    await ensure_no_other_active_workout(db, user.id)

    routine = await _load_routine(db, user.id, routine_id)
    meso_id, current_phase, micro_id, training_block_id = await _free_session_context(
        db, user.id, now
    )

    workout = WorkoutSession(
        client_uuid=str(uuid.uuid4()),
        app_user_id=user.id,
        source="free",
        status="active",
        routine_id=routine.id,
        plan_id=None,
        split_day_id=None,
        app_user_mesocycle_id=meso_id,
        mesocycle_phase=current_phase,
        app_user_microcycle_id=micro_id,
        training_block_id=training_block_id,
        notes=routine.notes,
        started_at=now,
    )
    db.add(workout)
    await db.flush()

    profile = (
        await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == user.id)
        )
    ).scalars().first()
    experience_level = profile.experience_level if profile else None
    settings = profile.settings if profile else None
    readiness_verdict = await readiness_repo.verdict_for_checkin(
        db, user.id, None, settings
    )
    phase_effort_tier = await progression_repo.resolve_phase_effort_tier(
        db,
        workout.app_user_mesocycle_id,
        workout.mesocycle_phase,
        training_block_id=workout.training_block_id,
    )

    superset_ids: dict[str, str] = {}
    for item in routine.exercises:
        fresh_superset = None
        if item.superset_group:
            fresh_superset = superset_ids.setdefault(
                item.superset_group, str(uuid.uuid4())
            )
        session_exercise = WorkoutSessionExercise(
            client_uuid=str(uuid.uuid4()),
            workout_session_id=workout.id,
            exercise_id=item.exercise_id,
            order_index=item.order_index,
            superset_group=fresh_superset,
            target_sets=item.target_sets,
            recommended_rep_min=item.rep_min,
            recommended_rep_max=item.rep_max,
            recommended_rir=item.target_rir,
            notes=item.notes,
        )
        db.add(session_exercise)
        await db.flush()
        session_exercise.exercise = item.exercise

        has_routine_range = item.rep_min is not None and item.rep_max is not None
        context = await progression_repo.build_context(
            db,
            session_exercise,
            user.id,
            experience_level,
            settings,
            rep_range_source=(
                progression_params.REP_SOURCE_PLAN
                if has_routine_range
                else progression_params.REP_SOURCE_FALLBACK
            ),
            phase_effort_tier=phase_effort_tier,
            readiness=readiness_verdict,
        )
        prescription = plan_exercise(
            context,
            override=override_for(settings, session_exercise.exercise_id),
        )
        progression_repo.persist_prescription(session_exercise, prescription)

    await db.commit()
    return (
        await db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == workout.id)
            .options(_workout_options())
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
