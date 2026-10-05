from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from api.main import app
from api.services.app_user_service import (
    get_current_app_user,
    get_current_app_user_allow_pending,
)
from api.services.import_service import normalize_external_name
from api.services.exercise_alias_identity import (
    normalized_external_name_sha256_from_normalized,
)
from api.services.import_service import save_alias
from api.services.models import AppUser, Exercise, ExerciseImportAlias


pytestmark = pytest.mark.asyncio


def _custom_exercise(user_id: int, suffix: str) -> Exercise:
    return Exercise(
        name=f"Alias integration {suffix} {uuid.uuid4().hex[:8]}",
        category="base",
        main_muscle_group="chest",
        secondary_muscle_groups=[],
        equipment_needed=["barbell"],
        difficulty="intermediate",
        source="custom",
        app_user_id=user_id,
    )


async def test_endpoint_requires_authentication(client) -> None:
    current_override = app.dependency_overrides.pop(get_current_app_user)
    pending_override = app.dependency_overrides.pop(
        get_current_app_user_allow_pending
    )
    try:
        response = await client.put(
            "/exercises/import-alias",
            json={
                "source": "strong",
                "external_name": "Bench Press",
                "exercise_id": 1,
            },
        )
    finally:
        app.dependency_overrides[get_current_app_user] = current_override
        app.dependency_overrides[get_current_app_user_allow_pending] = (
            pending_override
        )

    assert response.status_code in (401, 403)


async def test_unprefixed_alias_route_does_not_exist(client) -> None:
    response = await client.put(
        "/import-alias",
        json={
            "source": "strong",
            "external_name": "Bench Press",
            "exercise_id": 1,
        },
    )

    assert response.status_code == 404


async def test_first_int32_overflow_is_rejected_before_database_lookup(client) -> None:
    response = await client.put(
        "/exercises/import-alias",
        json={
            "source": "strong",
            "external_name": "Bench Press",
            "exercise_id": 2_147_483_648,
        },
    )

    assert response.status_code == 422


async def test_normalized_put_is_idempotent_and_updates_one_mapping(
    client,
    db,
    test_user,
) -> None:
    first_exercise = _custom_exercise(test_user.id, "first")
    second_exercise = _custom_exercise(test_user.id, "second")
    db.add_all([first_exercise, second_exercise])
    await db.commit()

    first = await client.put(
        "/exercises/import-alias",
        json={
            "source": "strong",
            "external_name": "  Stra\u00dfe\u00a0Press ",
            "exercise_id": first_exercise.id,
        },
    )
    repeated = await client.put(
        "/exercises/import-alias",
        json={
            "source": "strong",
            "external_name": "  Stra\u00dfe\u00a0Press ",
            "exercise_id": first_exercise.id,
        },
    )
    updated = await client.put(
        "/exercises/import-alias",
        json={
            "source": "strong",
            "external_name": "STRASSE\tPRESS",
            "exercise_id": second_exercise.id,
        },
    )

    assert first.status_code == repeated.status_code == updated.status_code == 200
    assert updated.json()["normalized_external_name"] == "strasse press"
    assert updated.json()["external_name"] == "STRASSE\tPRESS"
    assert updated.json()["exercise_id"] == second_exercise.id
    rows = (
        await db.execute(
            select(ExerciseImportAlias).where(
                ExerciseImportAlias.app_user_id == test_user.id,
                ExerciseImportAlias.source == "strong",
                ExerciseImportAlias.normalized_external_name == "strasse press",
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].external_name == "STRASSE\tPRESS"
    assert rows[0].exercise_id == second_exercise.id


async def test_same_raw_alias_is_isolated_by_source_and_user(client, db, test_user) -> None:
    raw_alias = f"Shared alias {uuid.uuid4().hex[:8]}"
    normalized_alias = normalize_external_name(raw_alias)
    mine = _custom_exercise(test_user.id, "mine")
    other_user = AppUser(
        firebase_uid=f"alias-other-{uuid.uuid4().hex}",
        email=f"alias-other-{uuid.uuid4().hex}@example.com",
    )
    db.add_all([mine, other_user])
    await db.flush()
    theirs = _custom_exercise(other_user.id, "theirs")
    db.add(theirs)
    await db.flush()
    db.add(
        ExerciseImportAlias(
            app_user_id=other_user.id,
            source="strong",
            external_name=raw_alias,
            normalized_external_name=normalized_alias,
            normalized_external_name_sha256=(
                normalized_external_name_sha256_from_normalized(normalized_alias)
            ),
            exercise_id=theirs.id,
        )
    )
    await db.commit()

    try:
        for source in ("strong", "hevy"):
            response = await client.put(
                "/exercises/import-alias",
                json={
                    "source": source,
                    "external_name": raw_alias,
                    "exercise_id": mine.id,
                },
            )
            assert response.status_code == 200, response.text

        rows = (
            await db.execute(
                select(ExerciseImportAlias).where(
                    ExerciseImportAlias.normalized_external_name == normalized_alias
                )
            )
        ).scalars().all()
        identities = {
            (row.app_user_id, row.source, row.exercise_id)
            for row in rows
        }
        assert identities == {
            (test_user.id, "strong", mine.id),
            (test_user.id, "hevy", mine.id),
            (other_user.id, "strong", theirs.id),
        }
    finally:
        await db.delete(other_user)
        await db.commit()


async def test_maximum_unicode_expansion_persists_without_truncating_identity(
    client,
    db,
    test_user,
) -> None:
    exercise = _custom_exercise(test_user.id, "unicode-expansion")
    db.add(exercise)
    await db.commit()
    raw_alias = "\ufdfa" * 200
    normalized_alias = normalize_external_name(raw_alias)

    response = await client.put(
        "/exercises/import-alias",
        json={
            "source": "notes",
            "external_name": raw_alias,
            "exercise_id": exercise.id,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["normalized_external_name"] == normalized_alias
    assert len(normalized_alias) == 3_600
    row = (
        await db.execute(
            select(ExerciseImportAlias).where(
                ExerciseImportAlias.app_user_id == test_user.id,
                ExerciseImportAlias.source == "notes",
            )
        )
    ).scalar_one()
    assert row.normalized_external_name == normalized_alias
    assert row.normalized_external_name_sha256 == (
        normalized_external_name_sha256_from_normalized(normalized_alias)
    )


async def test_service_returning_refreshes_an_already_loaded_alias(db, test_user) -> None:
    exercise = _custom_exercise(test_user.id, "populate-existing")
    normalized_alias = normalize_external_name("Stra\u00dfe Press")
    loaded_alias = ExerciseImportAlias(
        app_user_id=test_user.id,
        source="fitbod",
        external_name="Stra\u00dfe Press",
        normalized_external_name=normalized_alias,
        normalized_external_name_sha256=(
            normalized_external_name_sha256_from_normalized(normalized_alias)
        ),
        exercise=exercise,
    )
    db.add(loaded_alias)
    await db.commit()

    returned = await save_alias(
        session=db,
        app_user_id=test_user.id,
        source="fitbod",
        external_name="STRASSE\tPRESS",
        exercise_id=exercise.id,
    )

    assert returned is loaded_alias
    assert loaded_alias.external_name == "STRASSE\tPRESS"
    assert loaded_alias.exercise_id == exercise.id
    await db.commit()


async def test_legacy_import_mappings_persist_only_accessible_targets(
    client,
    db,
    test_user,
) -> None:
    own_alias = f"Owned mapping {uuid.uuid4().hex[:8]}"
    foreign_alias = f"Foreign mapping {uuid.uuid4().hex[:8]}"
    mine = _custom_exercise(test_user.id, "legacy-owned")
    other_user = AppUser(
        firebase_uid=f"alias-legacy-other-{uuid.uuid4().hex}",
        email=f"alias-legacy-other-{uuid.uuid4().hex}@example.com",
    )
    db.add_all([mine, other_user])
    await db.flush()
    theirs = _custom_exercise(other_user.id, "legacy-foreign")
    db.add(theirs)
    await db.commit()
    csv_text = (
        "Date,Workout Name,Exercise Name,Set Order,Weight,Reps\n"
        f"2026-08-22 10:00:00,Alias security,{own_alias},1,50,5\n"
        f"2026-08-22 10:00:00,Alias security,{foreign_alias},1,50,5\n"
    )

    try:
        response = await client.post(
            "/data/import/commit",
            json={
                "csv": csv_text,
                "unit": "kg",
                "mappings": {
                    own_alias: mine.id,
                    foreign_alias: theirs.id,
                },
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["saved_aliases"] == 1
        rows = (
            await db.execute(
                select(ExerciseImportAlias).where(
                    ExerciseImportAlias.app_user_id == test_user.id,
                    ExerciseImportAlias.external_name.in_([own_alias, foreign_alias]),
                )
            )
        ).scalars().all()
        assert [(row.external_name, row.exercise_id) for row in rows] == [
            (own_alias, mine.id)
        ]
    finally:
        await db.delete(other_user)
        await db.commit()


async def test_unknown_and_other_users_exercises_write_nothing(client, db, test_user) -> None:
    other_user = AppUser(
        firebase_uid=f"alias-private-{uuid.uuid4().hex}",
        email=f"alias-private-{uuid.uuid4().hex}@example.com",
    )
    db.add(other_user)
    await db.flush()
    private_exercise = _custom_exercise(other_user.id, "private")
    db.add(private_exercise)
    await db.commit()

    try:
        for exercise_id in (2_147_483_647, private_exercise.id):
            response = await client.put(
                "/exercises/import-alias",
                json={
                    "source": "notes",
                    "external_name": f"Must not persist {exercise_id}",
                    "exercise_id": exercise_id,
                },
            )
            assert response.status_code == 404

        count = await db.scalar(
            select(func.count())
            .select_from(ExerciseImportAlias)
            .where(ExerciseImportAlias.app_user_id == test_user.id)
        )
        assert count == 0
    finally:
        await db.delete(other_user)
        await db.commit()
