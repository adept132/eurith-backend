from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from api.services.models import WorkoutSession, WorkoutSessionExercise

pytestmark = pytest.mark.asyncio


async def _create_exercise(client) -> int:
    response = await client.post(
        "/exercises",
        json={
            "name": f"P1-13 {uuid.uuid4().hex[:8]}",
            "main_muscle_group": "Грудь",
            "client_uuid": uuid.uuid4().hex,
        },
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def _snapshot(exercise_id: int, client_uuid: str) -> dict:
    return {
        "client_uuid": client_uuid,
        "source": "free",
        "status": "paused",
        "notes": "Хорошая тренировка",
        "started_at": "2026-08-22T10:00:00Z",
        "paused_at": "2026-08-22T10:10:00Z",
        "paused_duration_seconds": 0,
        "finished_at": None,
        "exercises": [
            {
                "client_uuid": f"{client_uuid}-exercise",
                "exercise_id": exercise_id,
                "order_index": 0,
                "notes": "Лопатки сведены",
                "sets": [
                    {
                        "client_uuid": f"{client_uuid}-set",
                        "set_number": 1,
                        "weight": 100,
                        "reps": 5,
                        "notes": "Контрольная пауза",
                        "is_completed": True,
                    }
                ],
            }
        ],
    }


async def test_paused_sync_resume_finish_is_one_monotonic_workout(
    client, auth_headers, db, test_user
):
    exercise_id = await _create_exercise(client)
    client_uuid = f"p113-{uuid.uuid4().hex}"
    paused = _snapshot(exercise_id, client_uuid)

    first = await client.post("/sync/workouts", headers=auth_headers, json=paused)
    assert first.status_code == 200, first.text
    workout_id = first.json()["id_map"][client_uuid]
    assert first.json()["workout"]["status"] == "paused"

    resumed = await client.post(
        f"/workouts/{workout_id}/resume", headers=auth_headers
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "active"

    finished = {
        **paused,
        "server_id": workout_id,
        "base_version": first.json()["sync_version"],
        "status": "finished",
        "paused_at": None,
        "paused_duration_seconds": 60,
        "finished_at": "2026-08-22T10:30:00Z",
    }
    finish_response = await client.post(
        "/sync/workouts", headers=auth_headers, json=finished
    )
    assert finish_response.status_code == 200, finish_response.text
    stable_finished_at = finish_response.json()["workout"]["finished_at"]

    replay = {
        **finished,
        "base_version": finish_response.json()["sync_version"],
        "finished_at": "2026-08-22T11:00:00Z",
    }
    replay_response = await client.post(
        "/sync/workouts", headers=auth_headers, json=replay
    )
    assert replay_response.status_code == 200, replay_response.text
    assert replay_response.json()["workout"]["finished_at"] == stable_finished_at

    count = await db.scalar(
        select(func.count()).select_from(WorkoutSession).where(
            WorkoutSession.app_user_id == test_user.id,
            WorkoutSession.client_uuid == client_uuid,
        )
    )
    assert count == 1

    row = (
        await db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == workout_id)
            .options(
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.sets
                )
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "finished"
    assert row.notes == "Хорошая тренировка"
    assert row.exercises[0].notes == "Лопатки сведены"
    assert row.exercises[0].sets[0].notes == "Контрольная пауза"
