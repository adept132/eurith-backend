import uuid

import pytest
from sqlalchemy import select

from api.schemas.gym_profiles import GymProfilePayload
from api.services.models import AppUser, AppUserProfile, GymProfile
from api.services.gym_profiles import (
    GymNotFound,
    GymRevisionConflict,
    delete_gym_profile,
    list_gym_profiles,
    put_gym_profile,
    set_active_gym_profile,
)


def payload(name="Garage", revision=0, equipment=None):
    return GymProfilePayload(
        name=name, equipment=equipment or ["barbell"], bars=[], discs=[], steps=[],
        expected_revision=revision,
    )


async def other_user(db):
    user = AppUser(
        firebase_uid=f"other-{uuid.uuid4()}",
        email=f"other-{uuid.uuid4()}@example.com",
    )
    db.add(user)
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_owner_scoped_create_retry_update_retry_and_stale_conflict(db, test_user):
    gym_id = uuid.uuid4()
    first = payload()
    created = await put_gym_profile(db, test_user.id, gym_id, first)
    retried = await put_gym_profile(db, test_user.id, gym_id, first)
    assert created.id == gym_id and created.revision == 1
    assert retried.revision == 1

    changed = payload(name="Garage Plus", revision=1, equipment=["barbell", "dumbbell"])
    updated = await put_gym_profile(db, test_user.id, gym_id, changed)
    retried_update = await put_gym_profile(db, test_user.id, gym_id, changed)
    assert updated.revision == retried_update.revision == 2

    with pytest.raises(GymRevisionConflict) as error:
        await put_gym_profile(db, test_user.id, gym_id, payload(name="Stale", revision=1))
    assert error.value.current.id == gym_id
    assert error.value.current.revision == 2

    foreign = await other_user(db)
    with pytest.raises(GymNotFound):
        await put_gym_profile(db, foreign.id, gym_id, changed)
    assert [gym.id for gym in await list_gym_profiles(db, foreign.id)] == []
    assert [gym.id for gym in await list_gym_profiles(db, test_user.id)] == [gym_id]


@pytest.mark.asyncio
async def test_delete_is_soft_idempotent_and_clears_active_selection(db, test_user):
    gym_id = uuid.uuid4()
    gym = await put_gym_profile(db, test_user.id, gym_id, payload())
    await set_active_gym_profile(db, test_user.id, gym_id)
    deleted = await delete_gym_profile(db, test_user.id, gym_id, expected_revision=1)
    retried = await delete_gym_profile(db, test_user.id, gym_id, expected_revision=1)
    assert deleted.revision == retried.revision == 2
    assert deleted.deleted_at is not None
    assert await list_gym_profiles(db, test_user.id) == []
    profile = (await db.execute(select(AppUserProfile).where(
        AppUserProfile.app_user_id == test_user.id
    ))).scalar_one()
    assert profile.settings.get("active_gym_profile_id") is None
    persisted = await db.get(GymProfile, gym.id)
    assert persisted is not None and persisted.deleted_at is not None


@pytest.mark.asyncio
async def test_foreign_or_deleted_gym_cannot_be_selected_or_deleted(db, test_user):
    owner = await other_user(db)
    foreign_gym = await put_gym_profile(db, owner.id, uuid.uuid4(), payload())
    foreign_gym_id = foreign_gym.id
    with pytest.raises(GymNotFound):
        await set_active_gym_profile(db, test_user.id, foreign_gym_id)
    with pytest.raises(GymNotFound):
        await delete_gym_profile(db, test_user.id, foreign_gym_id, expected_revision=1)

    own = await put_gym_profile(db, test_user.id, uuid.uuid4(), payload())
    await delete_gym_profile(db, test_user.id, own.id, expected_revision=1)
    with pytest.raises(GymNotFound):
        await set_active_gym_profile(db, test_user.id, own.id)
    await set_active_gym_profile(db, test_user.id, None)


@pytest.mark.asyncio
async def test_active_name_conflicts_but_deleted_name_can_be_reused(db, test_user):
    first = await put_gym_profile(db, test_user.id, uuid.uuid4(), payload("Studio"))
    with pytest.raises(GymRevisionConflict, match="name"):
        await put_gym_profile(db, test_user.id, uuid.uuid4(), payload("Studio"))
    await delete_gym_profile(db, test_user.id, first.id, expected_revision=1)
    replacement = await put_gym_profile(db, test_user.id, uuid.uuid4(), payload("Studio"))
    assert replacement.id != first.id
    assert replacement.deleted_at is None
