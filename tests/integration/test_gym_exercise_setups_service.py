from decimal import Decimal
import uuid

import pytest
from api.schemas.gym_profiles import GymExerciseSetupPayload
from api.services.models import AppUser, Exercise, GymProfile
from api.services.gym_exercise_setups import (
    GymExerciseSetupNotFound,
    GymExerciseSetupRevisionConflict,
    list_gym_exercise_setups,
    put_gym_exercise_setup,
    delete_gym_exercise_setup,
)
from api.services.gym_profiles import delete_gym_profile


def setup_payload(mode="stack", revision=0, *, is_available=True, step=2.5,
                  plate_inventory=None, basis=None, base_weight=None):
    basis = basis or ("displayed" if mode == "stack" else "plates_only")
    return GymExerciseSetupPayload(
        id=uuid.uuid4(), is_available=is_available, is_preferred=False,
        step_value=step, step_unit="kg", loading_sides=1, base_weight=base_weight,
        weight_basis=basis, plate_inventory=plate_inventory, expected_revision=revision,
    ).bind_mode(mode)


async def owner_gym(db, owner_id):
    gym = GymProfile(app_user_id=owner_id, name=f"Gym {uuid.uuid4()}")
    db.add(gym)
    await db.commit()
    await db.refresh(gym)
    return gym


async def exercise(db, owner_id=None):
    row = Exercise(
        name=f"Exercise {uuid.uuid4()}", category="strength", fatigue_tier=2,
        main_muscle_group="chest", secondary_muscle_groups=[], equipment_needed=[],
        difficulty="beginner", source="test", app_user_id=owner_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def other_user(db):
    user = AppUser(firebase_uid=f"other-{uuid.uuid4()}", email=f"other-{uuid.uuid4()}@example.com")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.mark.asyncio
async def test_owner_scope_exercise_reference_and_modes(db, test_user):
    gym = await owner_gym(db, test_user.id)
    gym_id = gym.id
    global_exercise = await exercise(db)
    global_exercise_id = global_exercise.id
    user_exercise = await exercise(db, test_user.id)
    user_exercise_id = user_exercise.id
    foreign_user = await other_user(db)
    foreign_exercise = await exercise(db, foreign_user.id)
    foreign_exercise_id = foreign_exercise.id

    stack = await put_gym_exercise_setup(db, test_user.id, gym_id, "global", global_exercise_id, setup_payload())
    stack_source = stack.exercise_source
    plate = await put_gym_exercise_setup(db, test_user.id, gym_id, "user", user_exercise_id, setup_payload("plate_loaded"))
    plate_source = plate.exercise_source
    assert stack_source == "global" and plate_source == "user"
    assert {row.load_mode for row in await list_gym_exercise_setups(db, test_user.id, gym_id)} == {"stack", "plate_loaded"}
    with pytest.raises(GymExerciseSetupNotFound):
        await put_gym_exercise_setup(db, test_user.id, gym_id, "user", foreign_exercise_id, setup_payload())
    with pytest.raises(GymExerciseSetupNotFound):
        await put_gym_exercise_setup(db, test_user.id, gym_id, "global", user_exercise_id, setup_payload())
    from api.services.gym_exercise_setups import require_exercise_reference
    with pytest.raises(GymExerciseSetupNotFound):
        await require_exercise_reference(db, test_user.id, "invalid", global_exercise_id)


@pytest.mark.asyncio
async def test_revisions_retries_and_active_tuple_collision(db, test_user):
    gym = await owner_gym(db, test_user.id)
    gym_id = gym.id
    ex = await exercise(db)
    exercise_id = ex.id
    original = setup_payload(step=2.3)
    created = await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, original)
    retried = await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, original)
    assert created.revision == retried.revision == 1

    changed = setup_payload(revision=1, is_available=False)
    changed.id = original.id
    updated = await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, changed)
    assert updated.revision == 2
    assert (await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, changed)).revision == 2
    stale = setup_payload(revision=1, is_available=True)
    stale.id = original.id
    with pytest.raises(GymExerciseSetupRevisionConflict) as conflict:
        await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, stale)
    assert conflict.value.current.revision == 2

    collision = setup_payload()
    with pytest.raises(GymExerciseSetupRevisionConflict) as conflict:
        await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, collision)
    assert conflict.value.current.id == original.id


@pytest.mark.asyncio
async def test_fractional_numeric_retry_uses_database_numeric_rounding(db, test_user):
    gym = await owner_gym(db, test_user.id)
    gym_id = gym.id
    ex = await exercise(db)
    exercise_id = ex.id
    original = setup_payload(step=1.2345)
    created = await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, original)
    assert Decimal(str(created.step_value)) == Decimal("1.235")
    retried = await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, original)
    assert retried.revision == 1


@pytest.mark.asyncio
async def test_create_persists_plate_inventory_and_exact_retry_is_idempotent(db, test_user):
    gym = await owner_gym(db, test_user.id)
    gym_id = gym.id
    ex = await exercise(db)
    exercise_id = ex.id
    inventory = [{"weight": 1.25, "count": 4}, {"weight": 2.5, "count": 2}]
    original = setup_payload("plate_loaded", plate_inventory=inventory)
    created = await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, original)
    assert created.plate_inventory == inventory
    retried = await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, original)
    assert retried.revision == 1 and retried.plate_inventory == inventory


@pytest.mark.asyncio
async def test_plate_setup_including_start_weight_round_trips(db, test_user):
    gym = await owner_gym(db, test_user.id)
    ex = await exercise(db)
    payload = setup_payload(
        "plate_loaded", basis="including_start_weight", base_weight=20.0,
    )

    created = await put_gym_exercise_setup(
        db, test_user.id, gym.id, "global", ex.id, payload,
    )
    assert created.weight_basis == "including_start_weight"
    assert created.base_weight == Decimal("20")

    fetched = await list_gym_exercise_setups(db, test_user.id, gym.id)
    assert len(fetched) == 1
    assert fetched[0].weight_basis == "including_start_weight"
    assert fetched[0].base_weight == Decimal("20")


@pytest.mark.asyncio
async def test_setup_uuid_cannot_move_between_keys_and_gym_owner_isolation(db, test_user):
    gym = await owner_gym(db, test_user.id)
    other_gym = await owner_gym(db, test_user.id)
    gym_id, other_gym_id = gym.id, other_gym.id
    ex = await exercise(db)
    exercise_id = ex.id
    created = await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, setup_payload())
    created_id = created.id
    second_exercise = await exercise(db)
    second_exercise_id = second_exercise.id
    payload = setup_payload()
    payload.id = created_id
    with pytest.raises(GymExerciseSetupNotFound):
        await put_gym_exercise_setup(db, test_user.id, other_gym_id, "global", exercise_id, payload)
    with pytest.raises(GymExerciseSetupNotFound):
        await put_gym_exercise_setup(db, test_user.id, gym_id, "global", second_exercise_id, payload)
    wrong_mode = setup_payload("plate_loaded")
    wrong_mode.id = created_id
    with pytest.raises(GymExerciseSetupNotFound):
        await put_gym_exercise_setup(db, test_user.id, gym_id, "global", exercise_id, wrong_mode)
    foreign = await other_user(db)
    with pytest.raises(GymExerciseSetupNotFound):
        await list_gym_exercise_setups(db, foreign.id, gym_id)
    await delete_gym_profile(db, test_user.id, other_gym_id, 1)
    with pytest.raises(GymExerciseSetupNotFound):
        await list_gym_exercise_setups(db, test_user.id, other_gym_id)


@pytest.mark.asyncio
async def test_delete_retry_targets_archived_uuid_not_replacement(db, test_user):
    gym = await owner_gym(db, test_user.id)
    ex = await exercise(db)
    original_payload = setup_payload()
    old = await put_gym_exercise_setup(db, test_user.id, gym.id, "global", ex.id, original_payload)
    deleted = await delete_gym_exercise_setup(db, test_user.id, gym.id, "global", ex.id, "stack", old.id, 1)
    retried = await delete_gym_exercise_setup(db, test_user.id, gym.id, "global", ex.id, "stack", old.id, 1)
    assert deleted.revision == retried.revision == 2 and deleted.deleted_at is not None

    replacement_payload = setup_payload()
    replacement = await put_gym_exercise_setup(db, test_user.id, gym.id, "global", ex.id, replacement_payload)
    await delete_gym_exercise_setup(db, test_user.id, gym.id, "global", ex.id, "stack", old.id, 1)
    active = await list_gym_exercise_setups(db, test_user.id, gym.id)
    assert len(active) == 1 and active[0].id == replacement.id and active[0].deleted_at is None
