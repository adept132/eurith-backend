"""Resolve and validate context for new or explicitly reassigned workout sets."""

from copy import deepcopy
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.load_context import resolve_load_context
from api.services.models import (
    AppUserProfile, Exercise, ExerciseLoadPreference, GymExerciseSetup,
    GymProfile, WorkoutSessionExercise,
)


CONTEXT_KEYS = frozenset({"load_mode", "gym_profile_id", "setup_id", "load_snapshot"})


async def resolve_set_context(
    db: AsyncSession,
    owner_id: int,
    exercise: WorkoutSessionExercise,
    *,
    mode: str | None,
    gym_id: UUID | None,
    setup_id: UUID | None,
    supplied_snapshot: dict | None = None,
) -> dict:
    """Build a server-owned snapshot; never trust client loading parameters."""
    if mode is None:
        if setup_id is not None or supplied_snapshot is not None:
            raise ValueError("A load mode is required for a machine setup or snapshot")
        if gym_id is not None:
            gym = await db.get(GymProfile, gym_id)
            if gym is None or gym.app_user_id != owner_id or gym.deleted_at is not None:
                raise ValueError("Gym profile is unavailable")
        return dict(load_mode=None, gym_profile_id=gym_id, setup_id=None,
                    load_snapshot=None)

    gym = None
    if gym_id is not None:
        gym = await db.get(GymProfile, gym_id)
        if gym is None or gym.app_user_id != owner_id or gym.deleted_at is not None:
            raise ValueError("Gym profile is unavailable")

    catalog_exercise = await db.get(Exercise, exercise.exercise_id)
    if catalog_exercise is None or catalog_exercise.app_user_id not in (None, owner_id):
        raise ValueError("Exercise is unavailable")
    source = "user" if catalog_exercise.app_user_id is not None else "global"
    preference = (await db.execute(select(ExerciseLoadPreference).where(
        ExerciseLoadPreference.app_user_id == owner_id,
        ExerciseLoadPreference.exercise_source == source,
        ExerciseLoadPreference.exercise_id == exercise.exercise_id,
    ))).scalar_one_or_none()

    setup = None
    if setup_id is not None:
        setup = await db.get(GymExerciseSetup, setup_id)
        if (setup is None or gym is None or setup.gym_id != gym.id
                or setup.exercise_source != source or setup.exercise_id != exercise.exercise_id
                or setup.load_mode != mode or setup.deleted_at is not None
                or not setup.is_available):
            raise ValueError("Machine setup does not match the gym, exercise and mode")

    global_settings = None
    if gym is None:
        global_settings = (await db.execute(select(AppUserProfile.settings).where(
            AppUserProfile.app_user_id == owner_id,
        ))).scalar_one_or_none()

    context = resolve_load_context(
        catalog_exercise.equipment_needed or [], preference, gym,
        [setup] if setup is not None else [], mode, global_settings,
    )
    if context is None:
        raise ValueError("Load mode is not allowed for this exercise")
    snapshot = jsonable_encoder(vars(context))
    if supplied_snapshot is not None and supplied_snapshot != snapshot:
        raise ValueError("Load snapshot does not match the saved machine context")
    return dict(load_mode=mode, gym_profile_id=gym_id, setup_id=setup_id,
                load_snapshot=deepcopy(snapshot))
