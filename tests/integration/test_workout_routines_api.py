from __future__ import annotations

import asyncio

import uuid

import pytest
from sqlalchemy import select

from api.services.models import AppUser, WorkoutRoutine


pytestmark = pytest.mark.asyncio


async def _create_exercise(client, *, name_prefix: str = "Routine") -> int:
    response = await client.post(
        "/exercises",
        json={
            "name": f"{name_prefix} {uuid.uuid4().hex[:8]}",
            "main_muscle_group": "Грудь",
            "client_uuid": uuid.uuid4().hex,
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def _routine_payload(exercise_id: int, *, client_uuid: str | None = None) -> dict:
    return {
        "client_uuid": client_uuid or str(uuid.uuid4()),
        "name": "Push",
        "notes": "Свести лопатки",
        "sort_order": 0,
        "exercises": [
            {
                "exercise_id": exercise_id,
                "order_index": 0,
                "superset_group": None,
                "target_sets": 3,
                "set_kinds": ["normal", "normal", "amrap"],
                "rep_min": 8,
                "rep_max": 10,
                "target_rir": 2,
                "rest_seconds": 120,
                "notes": "Пауза внизу",
            }
        ],
    }


async def _finished_workout(client, exercise_id: int) -> int:
    started = await client.post("/workouts/start", json={"source": "free"})
    assert started.status_code == 200, started.text
    workout_id = started.json()["id"]

    added = await client.post(
        f"/workouts/{workout_id}/exercises",
        json={"exercise_id": exercise_id},
    )
    assert added.status_code == 200, added.text
    session_exercise_id = added.json()["exercises"][-1]["id"]

    performed = await client.post(
        f"/workout-session-exercises/{session_exercise_id}/sets",
        json={"weight": 82.5, "reps": 8, "effort_level": "medium"},
    )
    assert performed.status_code == 200, performed.text
    finished = await client.post(f"/workouts/{workout_id}/finish")
    assert finished.status_code == 200, finished.text
    return workout_id


async def test_routine_crud_returns_nested_exercises_in_order(client):
    first_exercise_id = await _create_exercise(client, name_prefix="Routine first")
    second_exercise_id = await _create_exercise(client, name_prefix="Routine second")
    payload = _routine_payload(first_exercise_id)
    payload["exercises"].append(
        {
            **payload["exercises"][0],
            "exercise_id": second_exercise_id,
            "order_index": 1,
            "notes": None,
        }
    )

    created = await client.post("/workout-routines", json=payload)
    assert created.status_code == 201, created.text
    routine_id = created.json()["id"]
    assert [item["exercise_id"] for item in created.json()["exercises"]] == [
        first_exercise_id,
        second_exercise_id,
    ]
    assert created.json()["exercises"][0]["exercise"]["id"] == first_exercise_id

    listed = await client.get("/workout-routines")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [routine_id]

    updated = await client.patch(
        f"/workout-routines/{routine_id}",
        json={
            "name": "  Push A  ",
            "sort_order": 3,
            "base_revision": created.json()["revision"],
            "exercises": list(reversed(payload["exercises"])),
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Push A"
    assert [item["order_index"] for item in updated.json()["exercises"]] == [0, 1]
    assert updated.json()["revision"] == created.json()["revision"] + 1

    deleted = await client.delete(
        f"/workout-routines/{routine_id}",
        params={"base_revision": updated.json()["revision"]},
    )
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(f"/workout-routines/{routine_id}")).status_code == 404


async def test_routine_patch_rejects_stale_revision(client):
    exercise_id = await _create_exercise(client, name_prefix="Routine revision")
    created = await client.post("/workout-routines", json=_routine_payload(exercise_id))
    assert created.status_code == 201, created.text
    routine = created.json()

    first = await client.patch(
        f"/workout-routines/{routine['id']}",
        json={"name": "First", "base_revision": routine["revision"]},
    )
    assert first.status_code == 200, first.text
    stale = await client.patch(
        f"/workout-routines/{routine['id']}",
        json={"name": "Stale", "base_revision": routine["revision"]},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["routine"]["name"] == "First"


async def test_concurrent_routine_patches_allow_only_one_revision_winner(client):
    exercise_id = await _create_exercise(client, name_prefix="Routine concurrent")
    created = await client.post("/workout-routines", json=_routine_payload(exercise_id))
    routine = created.json()

    first, second = await asyncio.gather(
        client.patch(
            f"/workout-routines/{routine['id']}",
            json={"name": "Device A", "base_revision": routine["revision"]},
        ),
        client.patch(
            f"/workout-routines/{routine['id']}",
            json={"name": "Device B", "base_revision": routine["revision"]},
        ),
    )

    assert sorted((first.status_code, second.status_code)) == [200, 409]


async def test_routine_delete_rejects_stale_revision_and_returns_current(client):
    exercise_id = await _create_exercise(client, name_prefix="Routine delete conflict")
    created = await client.post("/workout-routines", json=_routine_payload(exercise_id))
    routine = created.json()
    updated = await client.patch(
        f"/workout-routines/{routine['id']}",
        json={"name": "Remote edit", "base_revision": routine["revision"]},
    )
    assert updated.status_code == 200, updated.text

    stale_delete = await client.delete(
        f"/workout-routines/{routine['id']}",
        params={"base_revision": routine["revision"]},
    )

    assert stale_delete.status_code == 409, stale_delete.text
    assert stale_delete.json()["detail"]["routine"]["name"] == "Remote edit"
    assert (await client.get(f"/workout-routines/{routine['id']}")).status_code == 200


async def test_save_as_routine_drops_weights(client):
    exercise_id = await _create_exercise(client)
    workout_id = await _finished_workout(client, exercise_id)

    response = await client.post(
        f"/workouts/{workout_id}/save-as-routine",
        json={"name": "Push", "client_uuid": str(uuid.uuid4())},
    )

    assert response.status_code == 201, response.text
    assert "weight" not in response.text
    assert response.json()["exercises"][0]["target_sets"] == 1


async def test_user_cannot_read_another_users_routine(client, db):
    marker = uuid.uuid4().hex[:12]
    foreign_user = AppUser(
        firebase_uid=f"foreign-{marker}",
        email=f"foreign-{marker}@example.com",
        display_name="Foreign",
    )
    db.add(foreign_user)
    await db.flush()
    foreign_routine = WorkoutRoutine(
        app_user_id=foreign_user.id,
        client_uuid=str(uuid.uuid4()),
        name="Foreign routine",
        sort_order=0,
    )
    db.add(foreign_routine)
    await db.commit()

    response = await client.get(f"/workout-routines/{foreign_routine.id}")

    assert response.status_code == 404
    await db.delete(foreign_user)
    await db.commit()


async def test_routine_validates_name_order_and_client_uuid(client, db):
    exercise_id = await _create_exercise(client)
    payload = _routine_payload(exercise_id)

    blank = await client.post("/workout-routines", json={**payload, "name": "   "})
    assert blank.status_code == 422

    gap = await client.post(
        "/workout-routines",
        json={
            **payload,
            "client_uuid": str(uuid.uuid4()),
            "exercises": [{**payload["exercises"][0], "order_index": 1}],
        },
    )
    assert gap.status_code == 422

    first = await client.post("/workout-routines", json=payload)
    assert first.status_code == 201, first.text
    duplicate = await client.post("/workout-routines", json=payload)
    assert duplicate.status_code == 409

    routines = (await db.execute(select(WorkoutRoutine))).scalars().all()
    assert len([item for item in routines if item.client_uuid == payload["client_uuid"]]) == 1
