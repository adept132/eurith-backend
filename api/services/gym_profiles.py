"""Owner scoped persistence operations for saved gym profiles."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas.gym_profiles import GymProfilePayload
from api.services.models import AppUserProfile, GymProfile


class GymNotFound(Exception):
    """The requested gym does not exist for this owner or is deleted."""


class GymRevisionConflict(Exception):
    """A revision or active name conflicts with the current stored state."""

    def __init__(self, current: GymProfile, message: str = "gym profile revision conflict"):
        super().__init__(message)
        self.current = current


def _payload_values(payload: GymProfilePayload) -> dict:
    values = payload.model_dump(mode="json", exclude={"expected_revision"})
    return values


def _same_values(gym: GymProfile, values: dict) -> bool:
    return all(getattr(gym, key) == value for key, value in values.items())


async def list_gym_profiles(session: AsyncSession, app_user_id: int) -> list[GymProfile]:
    result = await session.execute(
        select(GymProfile)
        .where(GymProfile.app_user_id == app_user_id, GymProfile.deleted_at.is_(None))
        .order_by(GymProfile.name, GymProfile.id)
    )
    rows = list(result.scalars().all())
    await session.commit()
    return rows


async def put_gym_profile(
    session: AsyncSession,
    app_user_id: int,
    gym_id: UUID,
    payload: GymProfilePayload,
) -> GymProfile:
    values = _payload_values(payload)
    current = (await session.execute(
        select(GymProfile).where(GymProfile.id == gym_id).with_for_update()
    )).scalar_one_or_none()

    if current is not None and (current.app_user_id != app_user_id or current.deleted_at is not None):
        await session.rollback()
        raise GymNotFound(gym_id)

    if current is not None:
        # Retry recognition intentionally precedes revision checking: clients
        # may repeat a committed request carrying the now-previous revision.
        if _same_values(current, values):
            await session.commit()
            return current
        if payload.expected_revision != current.revision:
            await session.commit()
            raise GymRevisionConflict(current)
    elif payload.expected_revision != 0:
        await session.rollback()
        raise GymNotFound(gym_id)

    duplicate = (await session.execute(
        select(GymProfile).where(
            GymProfile.app_user_id == app_user_id,
            GymProfile.name == values["name"],
            GymProfile.deleted_at.is_(None),
            GymProfile.id != gym_id,
        ).with_for_update()
    )).scalar_one_or_none()
    if duplicate is not None:
        await session.commit()
        raise GymRevisionConflict(duplicate, f"active gym name already exists: {values['name']}")

    if current is None:
        current = GymProfile(id=gym_id, app_user_id=app_user_id, revision=1, **values)
        session.add(current)
    else:
        for key, value in values.items():
            setattr(current, key, value)
        current.revision += 1

    await session.commit()
    await session.refresh(current)
    return current


async def delete_gym_profile(
    session: AsyncSession,
    app_user_id: int,
    gym_id: UUID,
    expected_revision: int,
) -> GymProfile:
    current = (await session.execute(
        select(GymProfile).where(GymProfile.id == gym_id).with_for_update()
    )).scalar_one_or_none()
    if current is None or current.app_user_id != app_user_id:
        await session.rollback()
        raise GymNotFound(gym_id)
    if current.deleted_at is not None:
        await session.commit()
        return current
    if current.revision != expected_revision:
        await session.commit()
        raise GymRevisionConflict(current)

    current.deleted_at = datetime.now(UTC)
    current.revision += 1
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id).with_for_update()
    )).scalar_one_or_none()
    if profile is not None:
        settings = dict(profile.settings or {})
        if str(settings.get("active_gym_profile_id")) == str(gym_id):
            settings.pop("active_gym_profile_id", None)
            profile.settings = settings
    await session.commit()
    await session.refresh(current)
    return current


async def set_active_gym_profile(
    session: AsyncSession,
    app_user_id: int,
    gym_id: UUID | None,
) -> AppUserProfile:
    if gym_id is not None:
        gym = (await session.execute(
            select(GymProfile).where(
                GymProfile.id == gym_id,
                GymProfile.app_user_id == app_user_id,
                GymProfile.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if gym is None:
            await session.rollback()
            raise GymNotFound(gym_id)

    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id).with_for_update()
    )).scalar_one_or_none()
    if profile is None:
        profile = AppUserProfile(app_user_id=app_user_id, settings={})
        session.add(profile)
    settings = dict(profile.settings or {})
    if gym_id is None:
        settings.pop("active_gym_profile_id", None)
    else:
        settings["active_gym_profile_id"] = str(gym_id)
    profile.settings = settings
    await session.commit()
    await session.refresh(profile)
    return profile
