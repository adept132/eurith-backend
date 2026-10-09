"""Resolve and validate context for new or explicitly reassigned workout sets."""

from copy import deepcopy
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.load_context import resolve_load_context
from api.schemas.warmups import WarmupLoadContext
from api.services.models import (
    AppUserProfile, Exercise, ExerciseLoadPreference, GymExerciseSetup,
    GymProfile, WorkoutSessionExercise,
)


CONTEXT_KEYS = frozenset({"load_mode", "gym_profile_id", "setup_id", "load_snapshot"})



def _equipment_extension(snapshot, *, mode, gym_id, setup_id, canonical=None):
    """Validate additive calculator evidence separately from authoritative machine core."""
    if not isinstance(snapshot, dict) or "equipment" not in snapshot:
        raise ValueError("A validated equipment snapshot is required")
    equipment = WarmupLoadContext.model_validate(snapshot["equipment"])
    if (equipment.load_mode != mode or equipment.gym_profile_id != gym_id
            or equipment.setup_id != setup_id):
        raise ValueError("Equipment identity does not match the set context")
    if "display_unit" in snapshot and snapshot["display_unit"] not in ("kg", "lbs"):
        raise ValueError("Invalid equipment display unit")
    if mode is None:
        if equipment.kind not in ("barbell", "step", "manual"):
            raise ValueError("A machine equipment snapshot requires a load mode")
    elif canonical is not None:
        allowed = ("plate_loaded", "manual") if mode == "plate_loaded" else ("step", "manual")
        if (equipment.kind not in allowed
                or equipment.loading_sides != canonical["loading_sides"]
                or equipment.weight_basis != canonical["weight_basis"]
                or equipment.base_weight_kg != canonical["base_weight"]):
            raise ValueError("Equipment does not match the saved machine context")
        if equipment.step is not None and (equipment.step.value != canonical["step_value"]
                or equipment.step.unit != canonical["step_unit"]):
            raise ValueError("Equipment step does not match the saved machine context")
    return {key: deepcopy(snapshot[key]) for key in ("equipment", "display_unit") if key in snapshot}

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
        if setup_id is not None:
            raise ValueError("A load mode is required for a machine setup or snapshot")
        if gym_id is not None:
            gym = await db.get(GymProfile, gym_id)
            if gym is None or gym.app_user_id != owner_id or gym.deleted_at is not None:
                raise ValueError("Gym profile is unavailable")
        extension = None
        if supplied_snapshot is not None:
            extension = _equipment_extension(supplied_snapshot, mode=None, gym_id=gym_id, setup_id=None)
            if set(supplied_snapshot) - {"equipment", "display_unit"}:
                raise ValueError("A load mode is required for machine snapshot fields")
        return dict(load_mode=None, gym_profile_id=gym_id, setup_id=None,
                    load_snapshot=extension)

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
    if supplied_snapshot is not None:
        extension = {}
        supplied_core = supplied_snapshot
        if "equipment" in supplied_snapshot or "display_unit" in supplied_snapshot:
            extension = _equipment_extension(supplied_snapshot, mode=mode, gym_id=gym_id,
                                             setup_id=setup_id, canonical=snapshot)
            supplied_core = {key: value for key, value in supplied_snapshot.items()
                             if key not in ("equipment", "display_unit")}
        if supplied_core != snapshot:
            raise ValueError("Load snapshot does not match the saved machine context")
        snapshot.update(extension)
    return dict(load_mode=mode, gym_profile_id=gym_id, setup_id=setup_id,
                load_snapshot=deepcopy(snapshot))
