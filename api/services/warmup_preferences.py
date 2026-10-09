"""Owner scoped persistence for per-exercise warmup preferences."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas.warmups import WarmupDefaults, WarmupDefaultsPatch, WarmupOverride, WarmupPreferencePayload
from api.services.gym_exercise_setups import GymExerciseSetupNotFound, require_exercise_reference
from api.services.gym_profiles import _constraint_name
from api.services.models import ExerciseWarmupPreference


class ExerciseWarmupPreferenceNotFound(Exception):
    """The exercise or preference UUID is unavailable to this owner/key."""


class ExerciseWarmupPreferenceRevisionConflict(Exception):
    """A different preference or stale revision conflicts with current state."""

    def __init__(self, current: ExerciseWarmupPreference, message: str = "exercise warmup preference revision conflict"):
        super().__init__(message)
        self.current = current


def warmup_defaults(settings: dict | None) -> dict:
    """Expose complete defaults without modifying a legacy stored profile."""
    return WarmupDefaults.model_validate((settings or {}).get("warmup") or {}).model_dump(mode="json")


def merge_warmup_defaults(settings: dict | None, patch: WarmupDefaultsPatch) -> dict:
    values = warmup_defaults(settings)
    values.update(patch.model_dump(mode="json", exclude_unset=True))
    return WarmupDefaults.model_validate(values).model_dump(mode="json")


def _merged_override(current: ExerciseWarmupPreference | None, payload: WarmupPreferencePayload) -> dict:
    values = WarmupOverride.model_validate(current.override if current else {}).model_dump(mode="json")
    values.update(payload.override.model_dump(mode="json", exclude_unset=True))
    return values


def _same_values(current: ExerciseWarmupPreference, payload: WarmupPreferencePayload) -> bool:
    return current.override == _merged_override(current, payload)


async def list_warmup_preferences(session: AsyncSession, app_user_id: int) -> list[ExerciseWarmupPreference]:
    rows = list((await session.execute(
        select(ExerciseWarmupPreference).where(ExerciseWarmupPreference.app_user_id == app_user_id)
        .order_by(ExerciseWarmupPreference.exercise_source, ExerciseWarmupPreference.exercise_id)
    )).scalars().all())
    await session.commit()
    return rows


async def _require_reference(session: AsyncSession, app_user_id: int, exercise_source: str, exercise_id: int) -> None:
    try:
        await require_exercise_reference(session, app_user_id, exercise_source, exercise_id)
    except GymExerciseSetupNotFound as error:
        await session.rollback()
        raise ExerciseWarmupPreferenceNotFound((exercise_source, exercise_id)) from error


async def put_exercise_warmup_preference(
    session: AsyncSession,
    app_user_id: int,
    exercise_source: str,
    exercise_id: int,
    payload: WarmupPreferencePayload,
) -> ExerciseWarmupPreference:
    await _require_reference(session, app_user_id, exercise_source, exercise_id)

    current = (await session.execute(
        select(ExerciseWarmupPreference).where(
            ExerciseWarmupPreference.app_user_id == app_user_id,
            ExerciseWarmupPreference.exercise_source == exercise_source,
            ExerciseWarmupPreference.exercise_id == exercise_id,
        ).with_for_update()
    )).scalar_one_or_none()

    if current is not None and current.id != payload.id:
        await session.commit()
        raise ExerciseWarmupPreferenceRevisionConflict(current, "preference already exists for this exercise")

    by_id = (await session.execute(
        select(ExerciseWarmupPreference).where(ExerciseWarmupPreference.id == payload.id).with_for_update()
    )).scalar_one_or_none()
    if by_id is not None and (
        by_id.app_user_id != app_user_id
        or by_id.exercise_source != exercise_source
        or by_id.exercise_id != exercise_id
    ):
        await session.rollback()
        raise ExerciseWarmupPreferenceNotFound(payload.id)

    if current is not None and _same_values(current, payload):
        await session.commit()
        return current

    if current is None and payload.expected_revision != 0:
        await session.rollback()
        raise ExerciseWarmupPreferenceNotFound(payload.id)
    if current is not None and current.revision != payload.expected_revision:
        await session.commit()
        raise ExerciseWarmupPreferenceRevisionConflict(current)

    if current is None:
        current = ExerciseWarmupPreference(
            id=payload.id,
            app_user_id=app_user_id,
            exercise_source=exercise_source,
            exercise_id=exercise_id,
            override=_merged_override(None, payload),
            revision=1,
        )
        session.add(current)
    else:
        current.override = _merged_override(current, payload)
        current.revision += 1

    try:
        await session.commit()
    except IntegrityError as error:
        constraint = _constraint_name(error)
        if constraint not in {
            "uq_exercise_warmup_preferences_owner_exercise",
            "exercise_warmup_preferences_pkey",
        }:
            raise
        await session.rollback()
        current = (await session.execute(
            select(ExerciseWarmupPreference).where(
                ExerciseWarmupPreference.app_user_id == app_user_id,
                ExerciseWarmupPreference.exercise_source == exercise_source,
                ExerciseWarmupPreference.exercise_id == exercise_id,
            )
        )).scalar_one_or_none()
        if current is not None:
            if current.id == payload.id and _same_values(current, payload):
                await session.commit()
                return current
            await session.commit()
            raise ExerciseWarmupPreferenceRevisionConflict(current) from error

        by_id = (await session.execute(
            select(ExerciseWarmupPreference).where(ExerciseWarmupPreference.id == payload.id)
        )).scalar_one_or_none()
        if by_id is not None:
            await session.rollback()
            raise ExerciseWarmupPreferenceNotFound(payload.id) from error
        raise

    await session.refresh(current)
    return current
