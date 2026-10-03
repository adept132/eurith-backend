"""Owner scoped persistence for per-exercise loading preferences."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas.gym_profiles import ExerciseLoadPreferencePayload
from api.services.gym_exercise_setups import GymExerciseSetupNotFound, require_exercise_reference
from api.services.gym_profiles import _constraint_name
from api.services.models import ExerciseLoadPreference


class ExerciseLoadPreferenceNotFound(Exception):
    """The exercise or preference UUID is unavailable to this owner/key."""


class ExerciseLoadPreferenceRevisionConflict(Exception):
    """A different preference or stale revision conflicts with current state."""

    def __init__(self, current: ExerciseLoadPreference, message: str = "exercise load preference revision conflict"):
        super().__init__(message)
        self.current = current


def _same_values(current: ExerciseLoadPreference, payload: ExerciseLoadPreferencePayload) -> bool:
    return (
        current.enabled_modes == payload.enabled_modes
        and current.preferred_mode == payload.preferred_mode
    )


async def _require_reference(session: AsyncSession, app_user_id: int, exercise_source: str, exercise_id: int) -> None:
    try:
        await require_exercise_reference(session, app_user_id, exercise_source, exercise_id)
    except GymExerciseSetupNotFound as error:
        await session.rollback()
        raise ExerciseLoadPreferenceNotFound((exercise_source, exercise_id)) from error


async def get_exercise_load_preference(
    session: AsyncSession, app_user_id: int, exercise_source: str, exercise_id: int
) -> ExerciseLoadPreference | None:
    await _require_reference(session, app_user_id, exercise_source, exercise_id)
    result = await session.execute(
        select(ExerciseLoadPreference).where(
            ExerciseLoadPreference.app_user_id == app_user_id,
            ExerciseLoadPreference.exercise_source == exercise_source,
            ExerciseLoadPreference.exercise_id == exercise_id,
        )
    )
    row = result.scalar_one_or_none()
    await session.commit()
    return row


async def put_exercise_load_preference(
    session: AsyncSession,
    app_user_id: int,
    exercise_source: str,
    exercise_id: int,
    payload: ExerciseLoadPreferencePayload,
) -> ExerciseLoadPreference:
    await _require_reference(session, app_user_id, exercise_source, exercise_id)

    current = (await session.execute(
        select(ExerciseLoadPreference).where(
            ExerciseLoadPreference.app_user_id == app_user_id,
            ExerciseLoadPreference.exercise_source == exercise_source,
            ExerciseLoadPreference.exercise_id == exercise_id,
        ).with_for_update()
    )).scalar_one_or_none()

    by_id = (await session.execute(
        select(ExerciseLoadPreference).where(ExerciseLoadPreference.id == payload.id).with_for_update()
    )).scalar_one_or_none()
    if by_id is not None and (
        by_id.app_user_id != app_user_id
        or by_id.exercise_source != exercise_source
        or by_id.exercise_id != exercise_id
    ):
        await session.rollback()
        raise ExerciseLoadPreferenceNotFound(payload.id)

    if current is not None and current.id != payload.id:
        await session.commit()
        raise ExerciseLoadPreferenceRevisionConflict(current, "preference already exists for this exercise")

    if current is not None and _same_values(current, payload):
        await session.commit()
        return current

    if current is None and payload.expected_revision != 0:
        await session.rollback()
        raise ExerciseLoadPreferenceNotFound(payload.id)
    if current is not None and current.revision != payload.expected_revision:
        await session.commit()
        raise ExerciseLoadPreferenceRevisionConflict(current)

    if current is None:
        current = ExerciseLoadPreference(
            id=payload.id,
            app_user_id=app_user_id,
            exercise_source=exercise_source,
            exercise_id=exercise_id,
            enabled_modes=payload.enabled_modes,
            preferred_mode=payload.preferred_mode,
            revision=1,
        )
        session.add(current)
    else:
        current.enabled_modes = payload.enabled_modes
        current.preferred_mode = payload.preferred_mode
        current.revision += 1

    try:
        await session.commit()
    except IntegrityError as error:
        constraint = _constraint_name(error)
        if constraint not in {
            "uq_exercise_load_preferences_owner_exercise",
            "exercise_load_preferences_pkey",
        }:
            raise
        await session.rollback()
        current = (await session.execute(
            select(ExerciseLoadPreference).where(
                ExerciseLoadPreference.app_user_id == app_user_id,
                ExerciseLoadPreference.exercise_source == exercise_source,
                ExerciseLoadPreference.exercise_id == exercise_id,
            )
        )).scalar_one_or_none()
        if current is not None:
            if current.id == payload.id and _same_values(current, payload):
                await session.commit()
                return current
            await session.commit()
            raise ExerciseLoadPreferenceRevisionConflict(current) from error

        by_id = (await session.execute(
            select(ExerciseLoadPreference).where(ExerciseLoadPreference.id == payload.id)
        )).scalar_one_or_none()
        if by_id is not None:
            await session.rollback()
            raise ExerciseLoadPreferenceNotFound(payload.id) from error
        raise

    await session.refresh(current)
    return current
