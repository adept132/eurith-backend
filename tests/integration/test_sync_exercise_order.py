"""Regressions for the unique exercise-order index present in production."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def production_order_index(db):
    # Production has this legacy index even though it is not declared by the ORM.
    await db.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "ux_workout_session_exercises_session_order "
        "ON workout_session_exercises (workout_session_id, order_index)"
    ))
    await db.commit()
    yield
    await db.rollback()
    await db.execute(text("DROP INDEX ux_workout_session_exercises_session_order"))
    await db.commit()


async def test_sync_deletion_and_reorder_with_unique_order_index(
    client, production_order_index,
):
    catalog = await client.post("/exercises", json={
        "name": f"Sync order {uuid.uuid4().hex[:8]}",
        "main_muscle_group": "Грудь",
        "client_uuid": str(uuid.uuid4()),
    })
    assert catalog.status_code in (200, 201), catalog.text
    exercise_id = catalog.json()["id"]
    workout_id = str(uuid.uuid4())
    exercise_ids = [str(uuid.uuid4()) for _ in range(4)]

    def exercise(index: int, order: int, *, deleted: bool = False):
        return {
            "client_uuid": exercise_ids[index],
            "exercise_id": exercise_id,
            "order_index": order,
            "deleted": deleted,
            "sets": [],
        }

    snapshot = {
        "client_uuid": workout_id,
        "source": "free",
        "status": "active",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "exercises": [exercise(i, i) for i in range(4)],
    }
    created = await client.post("/sync/workouts", json=snapshot)
    assert created.status_code == 200, created.text

    # The mobile client sends active exercises first and deleted ones last.
    # The last active exercise moves into the deleted exercise's old slot.
    snapshot["base_version"] = created.json()["sync_version"]
    snapshot["exercises"] = [
        exercise(0, 0), exercise(1, 1), exercise(3, 2), exercise(2, 0, deleted=True),
    ]
    deleted = await client.post("/sync/workouts", json=snapshot)
    assert deleted.status_code == 200, deleted.text
    assert [item["client_uuid"] for item in deleted.json()["workout"]["exercises"]] == [
        exercise_ids[0], exercise_ids[1], exercise_ids[3],
    ]

    # A swap must also succeed despite the index checking each intermediate write.
    snapshot["base_version"] = deleted.json()["sync_version"]
    snapshot["exercises"] = [exercise(1, 0), exercise(0, 1), exercise(3, 2)]
    reordered = await client.post("/sync/workouts", json=snapshot)
    assert reordered.status_code == 200, reordered.text
    assert [item["client_uuid"] for item in reordered.json()["workout"]["exercises"]] == [
        exercise_ids[1], exercise_ids[0], exercise_ids[3],
    ]

    # Missing exercises in a partial snapshot remain on the server, even when
    # the incoming exercise takes one of their old positions.
    snapshot["base_version"] = reordered.json()["sync_version"]
    snapshot["exercises"] = [exercise(1, 2)]
    partial = await client.post("/sync/workouts", json=snapshot)
    assert partial.status_code == 200, partial.text
    partial_exercises = partial.json()["workout"]["exercises"]
    assert {item["client_uuid"] for item in partial_exercises} == {
        exercise_ids[0], exercise_ids[1], exercise_ids[3],
    }
    assert len({item["order_index"] for item in partial_exercises}) == 3

    # Corrupt duplicate positions are healed deterministically rather than
    # causing another production 500 for the same unique index.
    snapshot["base_version"] = partial.json()["sync_version"]
    snapshot["exercises"] = [exercise(1, 0), exercise(0, 0), exercise(3, 1)]
    duplicate = await client.post("/sync/workouts", json=snapshot)
    assert duplicate.status_code == 200, duplicate.text
    assert [item["client_uuid"] for item in duplicate.json()["workout"]["exercises"]] == [
        exercise_ids[1], exercise_ids[0], exercise_ids[3],
    ]
    assert [item["order_index"] for item in duplicate.json()["workout"]["exercises"]] == [
        0, 1, 2,
    ]
