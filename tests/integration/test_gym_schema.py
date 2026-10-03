import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from api.services.models import (
    AppUser,
    Exercise,
    ExerciseLoadPreference,
    GymExerciseSetup,
    GymProfile,
    WorkoutPlan,
    WorkoutPlanExercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)


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


@pytest.mark.asyncio
async def test_load_preference_preserves_client_generated_uuid(db, test_user):
    client_id = uuid.uuid4()
    preference = ExerciseLoadPreference(
        id=client_id,
        app_user_id=test_user.id,
        exercise_source="global",
        exercise_id=7,
        enabled_modes=["stack"],
        preferred_mode="stack",
    )
    db.add(preference)
    await db.commit()

    refetched_id = (
        await db.execute(
            select(ExerciseLoadPreference.id).where(
                ExerciseLoadPreference.id == client_id
            )
        )
    ).scalar_one()
    assert refetched_id == client_id


@pytest.mark.asyncio
async def test_load_preferences_use_source_and_exercise_as_distinct_keys(db, test_user):
    global_preference = ExerciseLoadPreference(
        app_user_id=test_user.id,
        exercise_source="global",
        exercise_id=7,
        enabled_modes=["stack", "plate_loaded"],
        preferred_mode="stack",
        revision=1,
    )
    user_preference = ExerciseLoadPreference(
        app_user_id=test_user.id,
        exercise_source="user",
        exercise_id=7,
        enabled_modes=["plate_loaded"],
        preferred_mode="plate_loaded",
        revision=2,
    )
    db.add_all([global_preference, user_preference])
    await db.commit()

    rows = (await db.execute(select(ExerciseLoadPreference))).scalars().all()
    assert {(row.exercise_source, row.exercise_id) for row in rows} == {
        ("global", 7),
        ("user", 7),
    }


@pytest.mark.asyncio
async def test_deleting_owner_cascades_load_preferences(db, test_user):
    preference = ExerciseLoadPreference(
        app_user_id=test_user.id,
        exercise_source="global",
        exercise_id=7,
        enabled_modes=["stack"],
        preferred_mode="stack",
        revision=1,
    )
    db.add(preference)
    await db.commit()

    await db.execute(delete(AppUser).where(AppUser.id == test_user.id))
    await db.commit()

    assert (
        await db.execute(
            select(ExerciseLoadPreference.id).where(
                ExerciseLoadPreference.id == preference.id
            )
        )
    ).scalar_one_or_none() is None


def test_workout_load_context_columns_are_nullable_and_snapshots_are_jsonb():
    from sqlalchemy.dialects.postgresql import JSONB

    fields = {
        WorkoutPlan: {"gym_profile_id": False, "gym_snapshot": True},
        WorkoutPlanExercise: {
            "load_mode": False,
            "setup_id": False,
            "load_snapshot": True,
        },
        WorkoutSession: {"gym_profile_id": False, "gym_snapshot": True},
        WorkoutSessionExercise: {"active_load_mode": False, "active_setup_id": False},
        WorkoutSessionSet: {
            "load_mode": False,
            "gym_profile_id": False,
            "setup_id": False,
            "load_snapshot": True,
            "shown_target_snapshot": True,
        },
    }
    for model, columns in fields.items():
        for name, is_json in columns.items():
            column = model.__table__.c[name]
            assert column.nullable
            if is_json:
                assert isinstance(column.type, JSONB)


@pytest.mark.asyncio
async def test_legacy_workout_rows_allow_null_context_and_keep_gym_snapshot_after_soft_delete(
    db, test_user
):
    exercise = Exercise(
        name=f"Context test {uuid.uuid4()}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        source="default",
    )
    gym = GymProfile(app_user_id=test_user.id, name=f"Snapshot gym {uuid.uuid4()}")
    plan = WorkoutPlan(
        app_user_id=test_user.id,
        name="Legacy plan",
        day_tag="push",
        micro_tag="medium",
        meso_tag="easy",
    )
    session = WorkoutSession(app_user_id=test_user.id, source="free")
    db.add_all([exercise, gym, plan, session])
    await db.flush()
    plan_exercise = WorkoutPlanExercise(plan_id=plan.id, exercise_id=exercise.id, order_index=0)
    session_exercise = WorkoutSessionExercise(
        workout_session_id=session.id, exercise_id=exercise.id, order_index=0
    )
    db.add_all([plan_exercise, session_exercise])
    await db.flush()
    session_set = WorkoutSessionSet(
        workout_session_exercise_id=session_exercise.id, set_number=1
    )
    db.add(session_set)
    await db.commit()

    assert plan.gym_profile_id is None and plan.gym_snapshot is None
    assert plan_exercise.load_mode is None and plan_exercise.load_snapshot is None
    assert session.gym_profile_id is None and session.gym_snapshot is None
    assert session_exercise.active_load_mode is None
    assert session_set.load_mode is None and session_set.load_snapshot is None

    snapshot = {"name": gym.name, "revision": 1}
    session.gym_profile_id = gym.id
    session.gym_snapshot = snapshot
    gym.deleted_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(session)
    assert session.gym_profile_id == gym.id
    assert session.gym_snapshot == snapshot
