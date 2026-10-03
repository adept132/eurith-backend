"""HTTP endpoints for owner gym profiles and loading settings."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.gym_profiles import (
    ActiveGymPayload,
    BoundGymExerciseSetup,
    ExerciseLoadPreferencePayload,
    ExerciseLoadPreferenceView,
    ExerciseSource,
    GymExerciseSetupDeletePayload,
    GymExerciseSetupPayload,
    GymExerciseSetupView,
    GymProfilePayload,
    GymProfileView,
    LoadMode,
    RevisionedDeletePayload,
)
from api.services.app_user_service import get_current_app_user
from api.services.exercise_load_preferences import (
    ExerciseLoadPreferenceNotFound,
    ExerciseLoadPreferenceRevisionConflict,
    get_exercise_load_preference,
    put_exercise_load_preference,
)
from api.services.gym_exercise_setups import (
    GymExerciseSetupNotFound,
    GymExerciseSetupRevisionConflict,
    delete_gym_exercise_setup,
    list_gym_exercise_setups,
    put_gym_exercise_setup,
)
from api.services.gym_profiles import (
    GymNotFound,
    GymRevisionConflict,
    delete_gym_profile,
    list_gym_profiles,
    put_gym_profile,
    set_active_gym_profile,
)
from api.services.models import (
    AppUser,
    AppUserProfile,
    ExerciseLoadPreference,
    GymExerciseSetup,
    GymProfile,
)


router = APIRouter(tags=["gym profiles"])


async def _active_gym_id(db: AsyncSession, app_user_id: int) -> str | None:
    profile = (await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    if profile is None:
        return None
    value = (profile.settings or {}).get("active_gym_profile_id")
    return str(value) if value else None


def _gym_view(row: GymProfile, active_id: str | None) -> GymProfileView:
    return GymProfileView(
        id=row.id,
        name=row.name,
        equipment=row.equipment,
        bars=row.bars,
        discs=row.discs,
        steps=row.steps,
        revision=row.revision,
        is_active=str(row.id) == active_id,
    )


def _setup_view(row: GymExerciseSetup) -> GymExerciseSetupView:
    return GymExerciseSetupView(
        id=row.id,
        gym_id=row.gym_id,
        exercise_source=row.exercise_source,
        exercise_id=row.exercise_id,
        mode=row.load_mode,
        is_available=row.is_available,
        is_preferred=row.is_preferred,
        step_value=row.step_value,
        step_unit=row.step_unit,
        loading_sides=row.loading_sides,
        base_weight=row.base_weight,
        weight_basis=row.weight_basis,
        plate_inventory=row.plate_inventory,
        revision=row.revision,
    )


def _preference_view(row: ExerciseLoadPreference) -> ExerciseLoadPreferenceView:
    return ExerciseLoadPreferenceView(
        id=row.id,
        exercise_source=row.exercise_source,
        exercise_id=row.exercise_id,
        enabled_modes=row.enabled_modes,
        preferred_mode=row.preferred_mode,
        revision=row.revision,
    )


def _conflict(error: Exception, current) -> HTTPException:
    row = current.model_dump(mode="json") if hasattr(current, "model_dump") else current
    if not isinstance(row, dict):
        row = {key: getattr(current, key) for key in current.__mapper__.column_attrs.keys()}
    # Use JSON-compatible values for ORM rows (UUIDs and Decimals included).
    if hasattr(current, "__mapper__"):
        if isinstance(current, GymProfile):
            row = _gym_view(current, None).model_dump(mode="json")
        elif isinstance(current, GymExerciseSetup):
            row = _setup_view(current).model_dump(mode="json")
        elif isinstance(current, ExerciseLoadPreference):
            row = _preference_view(current).model_dump(mode="json")
    return HTTPException(
        status_code=409,
        detail={"message": str(error), "current": row, "revision": current.revision},
    )


@router.get("/gym-profiles", response_model=list[GymProfileView])
async def get_gym_profiles(
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> list[GymProfileView]:
    rows = await list_gym_profiles(db, current_user.id)
    active_id = await _active_gym_id(db, current_user.id)
    return [_gym_view(row, active_id) for row in rows]


@router.put("/gym-profiles/active")
async def put_active_gym_profile(
    payload: ActiveGymPayload,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, UUID | None]:
    try:
        await set_active_gym_profile(db, current_user.id, payload.gym_profile_id)
    except GymNotFound as error:
        raise HTTPException(status_code=404, detail="Gym profile not found") from error
    return {"gym_profile_id": payload.gym_profile_id}


@router.put("/gym-profiles/{gym_id}", response_model=GymProfileView)
async def put_gym(
    gym_id: UUID,
    payload: GymProfilePayload,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> GymProfileView:
    try:
        row = await put_gym_profile(db, current_user.id, gym_id, payload)
    except GymNotFound as error:
        raise HTTPException(status_code=404, detail="Gym profile not found") from error
    except GymRevisionConflict as error:
        raise _conflict(error, error.current) from error
    return _gym_view(row, await _active_gym_id(db, current_user.id))


@router.delete("/gym-profiles/{gym_id}", response_model=GymProfileView)
async def delete_gym(
    gym_id: UUID,
    payload: RevisionedDeletePayload,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> GymProfileView:
    try:
        row = await delete_gym_profile(db, current_user.id, gym_id, payload.expected_revision)
    except GymNotFound as error:
        raise HTTPException(status_code=404, detail="Gym profile not found") from error
    except GymRevisionConflict as error:
        raise _conflict(error, error.current) from error
    return _gym_view(row, await _active_gym_id(db, current_user.id))


@router.get("/gym-profiles/{gym_id}/exercise-setups", response_model=list[GymExerciseSetupView])
async def get_gym_exercise_setups(
    gym_id: UUID,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> list[GymExerciseSetupView]:
    try:
        rows = await list_gym_exercise_setups(db, current_user.id, gym_id)
    except GymExerciseSetupNotFound as error:
        raise HTTPException(status_code=404, detail="Gym profile not found") from error
    return [_setup_view(row) for row in rows]


@router.put(
    "/gym-profiles/{gym_id}/exercise-setups/{source}/{exercise_id}/{mode}",
    response_model=GymExerciseSetupView,
)
async def put_gym_exercise_setup_route(
    gym_id: UUID,
    source: ExerciseSource,
    exercise_id: Annotated[int, Path(gt=0)],
    mode: LoadMode,
    payload: GymExerciseSetupPayload,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> GymExerciseSetupView:
    # URL mode is authoritative; bind it before the service sees the request.
    try:
        bound: BoundGymExerciseSetup = payload.bind_mode(mode)
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error
    try:
        row = await put_gym_exercise_setup(
            db, current_user.id, gym_id, source, exercise_id, bound
        )
    except GymExerciseSetupNotFound as error:
        raise HTTPException(status_code=404, detail="Gym or exercise setup not found") from error
    except GymExerciseSetupRevisionConflict as error:
        raise _conflict(error, error.current) from error
    return _setup_view(row)


@router.delete(
    "/gym-profiles/{gym_id}/exercise-setups/{source}/{exercise_id}/{mode}",
    response_model=GymExerciseSetupView,
)
async def delete_gym_exercise_setup_route(
    gym_id: UUID,
    source: ExerciseSource,
    exercise_id: Annotated[int, Path(gt=0)],
    mode: LoadMode,
    payload: GymExerciseSetupDeletePayload,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> GymExerciseSetupView:
    try:
        row = await delete_gym_exercise_setup(
            db,
            current_user.id,
            gym_id,
            source,
            exercise_id,
            mode,
            payload.id,
            payload.expected_revision,
        )
    except GymExerciseSetupNotFound as error:
        raise HTTPException(status_code=404, detail="Gym exercise setup not found") from error
    except GymExerciseSetupRevisionConflict as error:
        raise _conflict(error, error.current) from error
    return _setup_view(row)


@router.get(
    "/exercise-load-preferences/{source}/{exercise_id}",
    response_model=ExerciseLoadPreferenceView | None,
)
async def get_exercise_load_preference_route(
    source: ExerciseSource,
    exercise_id: Annotated[int, Path(gt=0)],
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> ExerciseLoadPreferenceView | None:
    try:
        row = await get_exercise_load_preference(db, current_user.id, source, exercise_id)
    except ExerciseLoadPreferenceNotFound as error:
        raise HTTPException(status_code=404, detail="Exercise not found") from error
    return _preference_view(row) if row is not None else None


@router.put(
    "/exercise-load-preferences/{source}/{exercise_id}",
    response_model=ExerciseLoadPreferenceView,
)
async def put_exercise_load_preference_route(
    source: ExerciseSource,
    exercise_id: Annotated[int, Path(gt=0)],
    payload: ExerciseLoadPreferencePayload,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> ExerciseLoadPreferenceView:
    try:
        row = await put_exercise_load_preference(
            db, current_user.id, source, exercise_id, payload
        )
    except ExerciseLoadPreferenceNotFound as error:
        raise HTTPException(status_code=404, detail="Exercise not found") from error
    except ExerciseLoadPreferenceRevisionConflict as error:
        raise _conflict(error, error.current) from error
    return _preference_view(row)
