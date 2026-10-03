import uuid

import pytest

from api.schemas.gym_profiles import ExerciseLoadPreferencePayload
from api.services.models import AppUser, Exercise
from api.services.exercise_load_preferences import (
    ExerciseLoadPreferenceNotFound,
    ExerciseLoadPreferenceRevisionConflict,
    get_exercise_load_preference,
    put_exercise_load_preference,
)


def preference_payload(*, revision=0, modes=None, preferred="stack", id=None):
    return ExerciseLoadPreferencePayload(
        id=id or uuid.uuid4(),
        enabled_modes=modes or ["stack"],
        preferred_mode=preferred,
        expected_revision=revision,
    )


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
async def test_global_and_owned_user_exercises_accept_both_modes(db, test_user):
    global_exercise = await exercise(db)
    user_exercise = await exercise(db, test_user.id)

    global_row = await put_exercise_load_preference(
        db, test_user.id, "global", global_exercise.id,
        preference_payload(modes=["stack", "plate_loaded"], preferred="plate_loaded"),
    )
    user_row = await put_exercise_load_preference(
        db, test_user.id, "user", user_exercise.id, preference_payload(),
    )

    assert (global_row.exercise_source, global_row.exercise_id, global_row.revision) == ("global", global_exercise.id, 1)
    assert global_row.enabled_modes == ["stack", "plate_loaded"]
    assert global_row.preferred_mode == "plate_loaded"
    assert (user_row.exercise_source, user_row.exercise_id, user_row.app_user_id, user_row.revision) == (
        "user", user_exercise.id, test_user.id, 1
    )
    assert (await get_exercise_load_preference(db, test_user.id, "global", global_exercise.id)).id == global_row.id


@pytest.mark.asyncio
async def test_foreign_missing_and_wrong_source_exercises_are_not_found(db, test_user):
    owned = await exercise(db, test_user.id)
    foreign_user = await other_user(db)
    foreign = await exercise(db, foreign_user.id)
    global_exercise = await exercise(db)

    for source, exercise_id in (("user", foreign.id), ("global", owned.id), ("global", 987654321), ("other", global_exercise.id)):
        with pytest.raises(ExerciseLoadPreferenceNotFound):
            await put_exercise_load_preference(db, test_user.id, source, exercise_id, preference_payload())


@pytest.mark.asyncio
async def test_create_retry_update_retry_and_stale_conflict(db, test_user):
    ex = await exercise(db)
    create = preference_payload()
    created = await put_exercise_load_preference(db, test_user.id, "global", ex.id, create)
    retried = await put_exercise_load_preference(db, test_user.id, "global", ex.id, create)
    assert created.id == create.id and retried.revision == 1

    update = preference_payload(revision=1, modes=["stack", "plate_loaded"], preferred="plate_loaded", id=create.id)
    updated = await put_exercise_load_preference(db, test_user.id, "global", ex.id, update)
    replay = await put_exercise_load_preference(db, test_user.id, "global", ex.id, update)
    assert updated.revision == replay.revision == 2

    stale = preference_payload(revision=1, id=create.id)
    with pytest.raises(ExerciseLoadPreferenceRevisionConflict) as conflict:
        await put_exercise_load_preference(db, test_user.id, "global", ex.id, stale)
    assert conflict.value.current.id == create.id and conflict.value.current.revision == 2


@pytest.mark.asyncio
async def test_other_uuid_for_same_key_conflicts_with_current_row(db, test_user):
    ex = await exercise(db)
    original = await put_exercise_load_preference(db, test_user.id, "global", ex.id, preference_payload())
    with pytest.raises(ExerciseLoadPreferenceRevisionConflict) as conflict:
        await put_exercise_load_preference(db, test_user.id, "global", ex.id, preference_payload())
    assert conflict.value.current.id == original.id


@pytest.mark.asyncio
async def test_preference_uuid_cannot_move_to_another_exercise_key(db, test_user):
    first, second = await exercise(db), await exercise(db)
    first_id, second_id = first.id, second.id
    original = await put_exercise_load_preference(db, test_user.id, "global", first_id, preference_payload())
    moved = preference_payload(revision=1, id=original.id)
    with pytest.raises(ExerciseLoadPreferenceNotFound):
        await put_exercise_load_preference(db, test_user.id, "global", second_id, moved)
    assert (await get_exercise_load_preference(db, test_user.id, "global", first_id)).revision == 1


@pytest.mark.asyncio
async def test_two_users_have_independent_preferences_for_same_global_exercise(db, test_user):
    ex = await exercise(db)
    second_user = await other_user(db)
    first = await put_exercise_load_preference(db, test_user.id, "global", ex.id, preference_payload())
    second = await put_exercise_load_preference(db, second_user.id, "global", ex.id, preference_payload())
    assert first.id != second.id and first.app_user_id != second.app_user_id
    assert (await get_exercise_load_preference(db, test_user.id, "global", ex.id)).id == first.id
    assert (await get_exercise_load_preference(db, second_user.id, "global", ex.id)).id == second.id
