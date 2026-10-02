import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from api.services.models import AppUser, GymExerciseSetup, GymProfile


@pytest.mark.asyncio
async def test_two_modes_one_exercise_one_gym(db, test_user):
    gym = GymProfile(app_user_id=test_user.id, name="Home gym")
    db.add(gym)
    await db.flush()

    stack = GymExerciseSetup(
        gym_id=gym.id,
        exercise_source="global",
        exercise_id=1,
        load_mode="stack",
        step_value=2.5,
        step_unit="kg",
        loading_sides=1,
        weight_basis="total",
    )
    plate_loaded = GymExerciseSetup(
        gym_id=gym.id,
        exercise_source="global",
        exercise_id=1,
        load_mode="plate_loaded",
        step_value=2.5,
        step_unit="kg",
        loading_sides=2,
        weight_basis="total",
    )
    db.add_all([stack, plate_loaded])
    await db.commit()

    rows = (
        await db.execute(
            select(GymExerciseSetup).where(GymExerciseSetup.gym_id == gym.id)
        )
    ).scalars().all()
    assert {row.load_mode for row in rows} == {"stack", "plate_loaded"}


@pytest.mark.asyncio
async def test_duplicate_active_same_mode_is_rejected(db, test_user):
    gym = GymProfile(app_user_id=test_user.id, name="Home gym")
    db.add(gym)
    await db.flush()
    setup_args = dict(
        gym_id=gym.id,
        exercise_source="global",
        exercise_id=1,
        load_mode="stack",
        step_value=2.5,
        step_unit="kg",
        loading_sides=1,
        weight_basis="total",
    )
    db.add(GymExerciseSetup(**setup_args))
    await db.flush()

    db.add(GymExerciseSetup(**setup_args))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@pytest.mark.asyncio
async def test_archived_setup_allows_new_uuid_for_replacement(db, test_user):
    gym = GymProfile(app_user_id=test_user.id, name="Home gym")
    db.add(gym)
    await db.flush()
    original = GymExerciseSetup(
        gym_id=gym.id,
        exercise_source="global",
        exercise_id=1,
        load_mode="plate_loaded",
        step_value=2.5,
        step_unit="kg",
        loading_sides=2,
        weight_basis="total",
    )
    db.add(original)
    await db.flush()
    original_id = original.id

    original.deleted_at = datetime.now(UTC)
    replacement = GymExerciseSetup(
        gym_id=gym.id,
        exercise_source="global",
        exercise_id=1,
        load_mode="plate_loaded",
        step_value=1.25,
        step_unit="kg",
        loading_sides=2,
        weight_basis="total",
    )
    db.add(replacement)
    await db.commit()

    assert original.id == original_id
    assert replacement.id != original_id
    assert (
        await db.execute(
            select(GymExerciseSetup.id).where(GymExerciseSetup.id == original_id)
        )
    ).scalar_one_or_none() == original_id


@pytest.mark.asyncio
async def test_negative_plate_count_is_rejected_by_database(db, test_user):
    gym = GymProfile(app_user_id=test_user.id, name="Home gym")
    db.add(gym)
    await db.flush()

    setup = GymExerciseSetup(
        gym_id=gym.id,
        exercise_source="global",
        exercise_id=1,
        load_mode="plate_loaded",
        step_value=2.5,
        step_unit="kg",
        loading_sides=2,
        weight_basis="total",
        plate_inventory=[{"weight": 2.5, "count": -1}],
    )
    db.add(setup)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


@pytest.mark.asyncio
async def test_deleting_owner_cascades_gym_entities(db, test_user):
    gym = GymProfile(app_user_id=test_user.id, name="Home gym")
    db.add(gym)
    await db.flush()
    setup = GymExerciseSetup(
        gym_id=gym.id,
        exercise_source="user",
        exercise_id=22,
        load_mode="plate_loaded",
        step_value=1.25,
        step_unit="kg",
        loading_sides=2,
        weight_basis="per_side",
    )
    db.add(setup)
    await db.commit()

    await db.execute(delete(AppUser).where(AppUser.id == test_user.id))
    await db.commit()

    assert (
        await db.execute(select(GymProfile.id).where(GymProfile.id == gym.id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(
            select(GymExerciseSetup.id).where(GymExerciseSetup.id == setup.id)
        )
    ).scalar_one_or_none() is None
