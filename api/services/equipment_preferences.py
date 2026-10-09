"""Owner/gym/exercise/context scoped equipment selections."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas.gym_profiles import EquipmentPreferencePayload
from api.services.gym_exercise_setups import GymExerciseSetupNotFound, _owned_gym, require_exercise_reference
from api.services.gym_profiles import _constraint_name
from api.services.models import ExerciseEquipmentPreference, GymProfile


class EquipmentPreferenceNotFound(Exception):
    """The gym, exercise or preference identity is unavailable to this owner."""


class EquipmentPreferenceRevisionConflict(Exception):
    def __init__(self, current: ExerciseEquipmentPreference, message: str = "equipment preference revision conflict"):
        super().__init__(message)
        self.current = current


class EquipmentSelectionUnavailable(Exception):
    """The selected bar or collar needs explicit re-selection."""


async def _require_gym(session: AsyncSession, app_user_id: int, gym_id: UUID) -> GymProfile:
    try:
        return await _owned_gym(session, app_user_id, gym_id)
    except GymExerciseSetupNotFound as error:
        raise EquipmentPreferenceNotFound(gym_id) from error


def _key(app_user_id: int, gym_id: UUID, source: str, exercise_id: int, context_key: str):
    return (
        ExerciseEquipmentPreference.app_user_id == app_user_id,
        ExerciseEquipmentPreference.gym_id == gym_id,
        ExerciseEquipmentPreference.exercise_source == source,
        ExerciseEquipmentPreference.exercise_id == exercise_id,
        ExerciseEquipmentPreference.context_key == context_key,
    )


def _same_values(current: ExerciseEquipmentPreference, payload: EquipmentPreferencePayload) -> bool:
    bar = payload.selected_bar.model_dump(mode="json") if payload.selected_bar else None
    return current.selected_bar == bar and current.selected_collar_id == payload.selected_collar_id


def _validate_inventory(gym: GymProfile, payload: EquipmentPreferencePayload) -> None:
    if payload.selected_bar is not None and not any(
        bar["weight"] == payload.selected_bar.weight and bar["unit"] == payload.selected_bar.unit
        and bar["count"] > 0 for bar in gym.bars
    ):
        raise EquipmentSelectionUnavailable("Selected bar is unavailable; select equipment again")
    if payload.selected_collar_id is not None and not any(
        str(collar["id"]) == str(payload.selected_collar_id) and collar["count"] > 0
        for collar in gym.collars
    ):
        raise EquipmentSelectionUnavailable("Selected collar is unavailable; select equipment again")


async def list_equipment_preferences(session: AsyncSession, app_user_id: int, gym_id: UUID) -> list[ExerciseEquipmentPreference]:
    await _require_gym(session, app_user_id, gym_id)
    rows = list((await session.execute(
        select(ExerciseEquipmentPreference).where(
            ExerciseEquipmentPreference.app_user_id == app_user_id,
            ExerciseEquipmentPreference.gym_id == gym_id,
        ).order_by(ExerciseEquipmentPreference.exercise_source, ExerciseEquipmentPreference.exercise_id,
                   ExerciseEquipmentPreference.context_key)
    )).scalars().all())
    await session.commit()
    return rows


async def put_equipment_preference(
    session: AsyncSession, app_user_id: int, gym_id: UUID,
    exercise_source: str, exercise_id: int, payload: EquipmentPreferencePayload,
) -> ExerciseEquipmentPreference:
    # Lock the gym through validation and commit so concurrent inventory changes
    # cannot commit an unavailable selection after its validation.
    gym = await _require_gym(session, app_user_id, gym_id)
    try:
        await require_exercise_reference(session, app_user_id, exercise_source, exercise_id)
    except GymExerciseSetupNotFound as error:
        await session.rollback()
        raise EquipmentPreferenceNotFound((exercise_source, exercise_id)) from error

    current = (await session.execute(select(ExerciseEquipmentPreference).where(
        *_key(app_user_id, gym_id, exercise_source, exercise_id, payload.context_key)
    ).with_for_update())).scalar_one_or_none()
    # Identity/scope are immutable. Do not lock a potentially foreign row:
    # opposite cross-gym UUID reuse would otherwise acquire row locks in
    # opposite order. The owned key is locked and creation races use the PK.
    by_id = (await session.execute(select(ExerciseEquipmentPreference).where(
        ExerciseEquipmentPreference.id == payload.id
    ))).scalar_one_or_none()
    if by_id is not None and (
        by_id.app_user_id != app_user_id or by_id.gym_id != gym_id
        or by_id.exercise_source != exercise_source or by_id.exercise_id != exercise_id
        or by_id.context_key != payload.context_key
    ):
        await session.rollback()
        raise EquipmentPreferenceNotFound(payload.id)
    if current is not None and current.id != payload.id:
        await session.commit()
        raise EquipmentPreferenceRevisionConflict(current, "preference already exists for this context")

    # Even an identical retry must tell the caller that deleted equipment needs
    # re-selection; GET preserves the stored selection without rewriting it.
    try:
        _validate_inventory(gym, payload)
    except EquipmentSelectionUnavailable:
        await session.rollback()
        raise
    if current is not None and _same_values(current, payload):
        await session.commit()
        return current
    if current is None and payload.expected_revision != 0:
        await session.rollback()
        raise EquipmentPreferenceNotFound(payload.id)
    if current is not None and current.revision != payload.expected_revision:
        await session.commit()
        raise EquipmentPreferenceRevisionConflict(current)

    bar = payload.selected_bar.model_dump(mode="json") if payload.selected_bar else None
    if current is None:
        current = ExerciseEquipmentPreference(id=payload.id, app_user_id=app_user_id, gym_id=gym_id,
            exercise_source=exercise_source, exercise_id=exercise_id, context_key=payload.context_key,
            selected_bar=bar, selected_collar_id=payload.selected_collar_id, revision=1)
        session.add(current)
    else:
        current.selected_bar = bar
        current.selected_collar_id = payload.selected_collar_id
        current.revision += 1
    try:
        await session.commit()
    except IntegrityError as error:
        if _constraint_name(error) not in {"uq_equipment_preferences_owner_context", "exercise_equipment_preferences_pkey"}:
            raise
        await session.rollback()
        current = (await session.execute(select(ExerciseEquipmentPreference).where(
            *_key(app_user_id, gym_id, exercise_source, exercise_id, payload.context_key)
        ))).scalar_one_or_none()
        if current is not None:
            if current.id == payload.id and _same_values(current, payload):
                await session.commit()
                return current
            await session.commit()
            raise EquipmentPreferenceRevisionConflict(current) from error
        by_id = (await session.execute(select(ExerciseEquipmentPreference).where(
            ExerciseEquipmentPreference.id == payload.id
        ))).scalar_one_or_none()
        if by_id is not None:
            await session.rollback()
            raise EquipmentPreferenceNotFound(payload.id) from error
        raise
    await session.refresh(current)
    return current
