"""Owner scoped persistence operations for exercise setups in saved gyms."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import null, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas.gym_profiles import BoundGymExerciseSetup
from api.services.gym_profiles import _constraint_name
from api.services.models import Exercise, GymExerciseSetup, GymProfile


class GymExerciseSetupNotFound(Exception):
    """The gym or exercise setup is unavailable to this owner."""


class GymExerciseSetupRevisionConflict(Exception):
    """A setup revision or active exercise mode conflicts with stored state."""

    def __init__(self, current: GymExerciseSetup, message: str = "gym exercise setup revision conflict"):
        super().__init__(message)
        self.current = current


def _setup_values(payload: BoundGymExerciseSetup) -> dict:
    values = payload.model_dump(exclude={"expected_revision", "mode"})
    values["load_mode"] = payload.mode
    return values


def _same_values(current: GymExerciseSetup, values: dict) -> bool:
    for key, value in values.items():
        stored = getattr(current, key)
        if key in {"step_value", "base_weight"}:
            stored = Decimal(str(stored)).quantize(Decimal("0.001")) if stored is not None else None
            value = Decimal(str(value)).quantize(Decimal("0.001")) if value is not None else None
        if stored != value:
            return False
    return True


async def _owned_gym(session: AsyncSession, app_user_id: int, gym_id: UUID) -> GymProfile:
    gym = (await session.execute(
        select(GymProfile).where(GymProfile.id == gym_id).with_for_update()
    )).scalar_one_or_none()
    if gym is None or gym.app_user_id != app_user_id or gym.deleted_at is not None:
        await session.rollback()
        raise GymExerciseSetupNotFound(gym_id)
    return gym


async def require_exercise_reference(
    session: AsyncSession, app_user_id: int, exercise_source: str, exercise_id: int
) -> Exercise:
    if exercise_source not in {"global", "user"}:
        raise GymExerciseSetupNotFound((exercise_source, exercise_id))
    owner_id = None if exercise_source == "global" else app_user_id
    exercise = (await session.execute(
        select(Exercise).where(
            Exercise.id == exercise_id,
            Exercise.app_user_id.is_(None) if owner_id is None else Exercise.app_user_id == owner_id,
        )
    )).scalar_one_or_none()
    if exercise is None:
        raise GymExerciseSetupNotFound((exercise_source, exercise_id))
    return exercise


async def list_gym_exercise_setups(
    session: AsyncSession, app_user_id: int, gym_id: UUID
) -> list[GymExerciseSetup]:
    await _owned_gym(session, app_user_id, gym_id)
    result = await session.execute(
        select(GymExerciseSetup).where(
            GymExerciseSetup.gym_id == gym_id,
            GymExerciseSetup.deleted_at.is_(None),
        ).order_by(
            GymExerciseSetup.exercise_source,
            GymExerciseSetup.exercise_id,
            GymExerciseSetup.load_mode,
            GymExerciseSetup.id,
        )
    )
    rows = list(result.scalars().all())
    await session.commit()
    return rows


async def put_gym_exercise_setup(
    session: AsyncSession,
    app_user_id: int,
    gym_id: UUID,
    exercise_source: str,
    exercise_id: int,
    payload: BoundGymExerciseSetup,
) -> GymExerciseSetup:
    await _owned_gym(session, app_user_id, gym_id)
    try:
        await require_exercise_reference(session, app_user_id, exercise_source, exercise_id)
    except GymExerciseSetupNotFound:
        await session.rollback()
        raise

    current = (await session.execute(
        select(GymExerciseSetup).where(GymExerciseSetup.id == payload.id).with_for_update()
    )).scalar_one_or_none()
    if current is not None and (
        current.gym_id != gym_id
        or current.exercise_source != exercise_source
        or current.exercise_id != exercise_id
        or current.load_mode != payload.mode
    ):
        await session.rollback()
        raise GymExerciseSetupNotFound(payload.id)

    values = _setup_values(payload)
    if current is not None and current.deleted_at is not None:
        await session.rollback()
        raise GymExerciseSetupNotFound(payload.id)
    if current is not None and _same_values(current, values):
        await session.commit()
        return current
    if current is None and payload.expected_revision != 0:
        await session.rollback()
        raise GymExerciseSetupNotFound(payload.id)
    if current is not None and current.revision != payload.expected_revision:
        await session.commit()
        raise GymExerciseSetupRevisionConflict(current)

    duplicate = (await session.execute(
        select(GymExerciseSetup).where(
            GymExerciseSetup.gym_id == gym_id,
            GymExerciseSetup.exercise_source == exercise_source,
            GymExerciseSetup.exercise_id == exercise_id,
            GymExerciseSetup.load_mode == payload.mode,
            GymExerciseSetup.deleted_at.is_(None),
            GymExerciseSetup.id != payload.id,
        ).with_for_update()
    )).scalar_one_or_none()
    if duplicate is not None:
        await session.commit()
        raise GymExerciseSetupRevisionConflict(duplicate, "active setup already exists for this exercise mode")

    if current is None:
        current = GymExerciseSetup(
            id=payload.id,
            gym_id=gym_id,
            exercise_source=exercise_source,
            exercise_id=exercise_id,
            revision=1,
            **{key: value for key, value in values.items() if key not in {"id", "plate_inventory"}},
        )
        session.add(current)
    else:
        for key, value in values.items():
            if key != "id":
                setattr(current, key, null() if key == "plate_inventory" and value is None else value)
        current.revision += 1

    try:
        await session.commit()
    except IntegrityError as error:
        if _constraint_name(error) != "uq_gym_setups_active_mode":
            raise
        await session.rollback()
        duplicate = (await session.execute(
            select(GymExerciseSetup).where(
                GymExerciseSetup.gym_id == gym_id,
                GymExerciseSetup.exercise_source == exercise_source,
                GymExerciseSetup.exercise_id == exercise_id,
                GymExerciseSetup.load_mode == payload.mode,
                GymExerciseSetup.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if duplicate is None:
            raise
        raise GymExerciseSetupRevisionConflict(duplicate, "active setup already exists for this exercise mode") from error
    await session.refresh(current)
    return current


async def delete_gym_exercise_setup(
    session: AsyncSession,
    app_user_id: int,
    gym_id: UUID,
    exercise_source: str,
    exercise_id: int,
    mode: str,
    setup_id: UUID,
    expected_revision: int,
) -> GymExerciseSetup:
    await _owned_gym(session, app_user_id, gym_id)
    current = (await session.execute(
        select(GymExerciseSetup).where(GymExerciseSetup.id == setup_id).with_for_update()
    )).scalar_one_or_none()
    if current is None or (
        current.gym_id != gym_id
        or current.exercise_source != exercise_source
        or current.exercise_id != exercise_id
        or current.load_mode != mode
    ):
        await session.rollback()
        raise GymExerciseSetupNotFound(setup_id)
    if current.deleted_at is not None:
        await session.commit()
        return current
    if current.revision != expected_revision:
        await session.commit()
        raise GymExerciseSetupRevisionConflict(current)
    current.deleted_at = datetime.now(UTC)
    current.revision += 1
    await session.commit()
    await session.refresh(current)
    return current
