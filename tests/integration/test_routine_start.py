from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.asyncio


def _payload(exercise_id: int) -> dict:
    return {
        "client_uuid": str(uuid.uuid4()),
        "name": "Current push",
        "sort_order": 0,
        "exercises": [
            {
                "exercise_id": exercise_id,
                "order_index": 0,
                "superset_group": "routine-superset-a",
                "target_sets": 3,
                "set_kinds": ["normal", "normal", "normal"],
                "rep_min": 8,
                "rep_max": 10,
                "target_rir": 2,
                "rest_seconds": 120,
                "notes": "Постоянная подсказка",
            }
        ],
    }


async def _create_routine(client, exercise_id: int) -> dict:
    response = await client.post("/workout-routines", json=_payload(exercise_id))
    assert response.status_code == 201, response.text
    return response.json()


async def test_start_routine_creates_free_session_with_current_prescription(
    client, auth_headers, seeded_history
):
    routine = await _create_routine(client, seeded_history.id)

    response = await client.post(
        f"/workout-routines/{routine['id']}/start", headers=auth_headers
    )

    assert response.status_code == 201, response.text
    workout = response.json()
    assert workout["source"] == "free"
    assert workout["routine_id"] == routine["id"]
    assert workout["plan_id"] is None
    assert len(workout["exercises"]) == 1
    started_exercise = workout["exercises"][0]
    assert started_exercise["client_uuid"]
    assert started_exercise["sets"] == []
    assert started_exercise["prescription"] is not None
    assert started_exercise["recommended_weight"] is not None
    assert started_exercise["prescription"]["basis"]["exercise_id"] == seeded_history.id
    assert started_exercise["superset_group"] != "routine-superset-a"

    unchanged = await client.get(f"/workout-routines/{routine['id']}")
    assert unchanged.status_code == 200
    assert unchanged.json() == routine


async def test_start_routine_rejects_active_session(client, seeded_history):
    routine = await _create_routine(client, seeded_history.id)
    active = await client.post("/workouts/start", json={"source": "free"})
    assert active.status_code == 200, active.text

    conflict = await client.post(f"/workout-routines/{routine['id']}/start")
    assert conflict.status_code == 409



async def test_each_routine_start_uses_fresh_session_exercise_uuid(
    client, seeded_history
):
    routine = await _create_routine(client, seeded_history.id)
    first = await client.post(f"/workout-routines/{routine['id']}/start")
    assert first.status_code == 201, first.text
    first_uuid = first.json()["exercises"][0]["client_uuid"]
    first_workout_id = first.json()["id"]
    finished = await client.post(f"/workouts/{first_workout_id}/finish")
    assert finished.status_code == 200, finished.text

    second = await client.post(f"/workout-routines/{routine['id']}/start")
    assert second.status_code == 201, second.text
    second_uuid = second.json()["exercises"][0]["client_uuid"]

    assert second.json()["id"] != first_workout_id
    assert second_uuid != first_uuid
    assert second.json()["exercises"][0]["sets"] == []
