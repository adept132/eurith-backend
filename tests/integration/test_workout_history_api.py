from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUser,
    Exercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)


T0 = datetime(2026, 8, 20, 18, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


@pytest_asyncio.fixture
async def history_exercise(db: AsyncSession, test_user: AppUser) -> Exercise:
    exercise = Exercise(
        name=f"History exercise {uuid.uuid4().hex[:8]}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(exercise)
    await db.commit()
    await db.refresh(exercise)
    return exercise


def history_body(
    exercise_id: int,
    *,
    base_revision: int = 0,
    entry_mode: str = "manual",
    weight: str = "80.00",
) -> dict:
    return {
        "client_uuid": "history-workout-1",
        "base_revision": base_revision,
        "entry_mode": entry_mode,
        "started_at": T0.isoformat(),
        "finished_at": T1.isoformat(),
        "notes": "Session note",
        "session_rpe": 7.5,
        "exercises": [
            {
                "client_uuid": "history-exercise-1",
                "exercise_id": exercise_id,
                "order_index": 0,
                "superset_group": None,
                "notes": "Exercise note",
                "sets": [
                    {
                        "client_uuid": "history-set-1",
                        "set_number": 1,
                        "set_type": "normal",
                        "weight": weight,
                        "reps": 8,
                        "effort_level": "medium",
                        "notes": "Set note",
                        "is_completed": True,
                    }
                ],
            }
        ],
    }


async def _create_finished_workout(
    db: AsyncSession,
    user_id: int,
    exercise_id: int,
) -> WorkoutSession:
    workout = WorkoutSession(
        app_user_id=user_id,
        client_uuid=f"workout-{uuid.uuid4().hex}",
        source="plan",
        status="finished",
        entry_mode="live",
        revision=1,
        started_at=T0,
        finished_at=T1,
    )
    db.add(workout)
    await db.flush()
    session_exercise = WorkoutSessionExercise(
        workout_session_id=workout.id,
        client_uuid="history-exercise-1",
        exercise_id=exercise_id,
        order_index=0,
        recommended_rir=2,
        recommended_weight=80,
        recommended_rep_min=6,
        recommended_rep_max=10,
        target_sets=3,
        prescription={"target_reps": 8},
        live_prescription={"target_reps": 9},
    )
    db.add(session_exercise)
    await db.flush()
    db.add(
        WorkoutSessionSet(
            workout_session_exercise_id=session_exercise.id,
            client_uuid="history-set-1",
            set_number=1,
            set_type="normal",
            weight=80,
            reps=8,
            effort_level="medium",
            is_completed=True,
            is_max_reps=True,
        )
    )
    await db.commit()
    await db.refresh(workout)
    return workout


@pytest.mark.asyncio
async def test_manual_create_is_finished(client, history_exercise: Exercise):
    response = await client.post(
        "/workouts/history", json=history_body(history_exercise.id)
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["workout"]["status"] == "finished"
    assert payload["workout"]["entry_mode"] == "manual"
    assert payload["workout"]["revision"] == 0
    assert payload["recalculation_status"] == "pending"


@pytest.mark.asyncio
async def test_manual_create_replay_is_idempotent(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    body = history_body(history_exercise.id)

    first = await client.post("/workouts/history", json=body)
    second = await client.post("/workouts/history", json=body)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["workout"]["id"] == first.json()["workout"]["id"]
    rows = list(
        (
            await db.execute(
                select(WorkoutSession).where(
                    WorkoutSession.app_user_id == test_user.id,
                    WorkoutSession.client_uuid == body["client_uuid"],
                )
            )
        ).scalars()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_manual_create_changed_replay_returns_typed_conflict(
    client, history_exercise: Exercise
):
    body = history_body(history_exercise.id)
    first = await client.post("/workouts/history", json=body)
    changed = {**body, "notes": "changed after a lost response"}

    second = await client.post("/workouts/history", json=changed)

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["error"] == "history_idempotency_conflict"
    assert detail["server_draft"]["notes"] == "Session note"


@pytest.mark.asyncio
async def test_manual_create_rejects_client_uuid_owned_by_live_workout(
    client,
    db: AsyncSession,
    test_user: AppUser,
    history_exercise: Exercise,
):
    live = await _create_finished_workout(db, test_user.id, history_exercise.id)
    body = history_body(history_exercise.id)
    body["client_uuid"] = live.client_uuid

    response = await client.post("/workouts/history", json=body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "history_idempotency_conflict"


@pytest.mark.asyncio
async def test_history_list_returns_completed_owned_workouts(
    client, history_exercise: Exercise
):
    created = await client.post(
        "/workouts/history", json=history_body(history_exercise.id)
    )
    workout_id = created.json()["workout"]["id"]

    response = await client.get("/workouts/history")

    assert response.status_code == 200, response.text
    items = response.json()["workouts"]
    assert len(items) == 1
    assert items[0] | {
        "id": workout_id,
        "entry_mode": "manual",
        "revision": 0,
        "exercises_count": 1,
        "sets_count": 1,
    } == items[0]


@pytest.mark.asyncio
async def test_patch_rejects_stale_revision_with_server_draft(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    body = history_body(
        history_exercise.id, base_revision=workout.revision - 1, entry_mode="live"
    )

    response = await client.patch(f"/workouts/{workout.id}/history", json=body)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "history_revision_conflict"
    assert detail["server_revision"] == 1
    assert detail["server_draft"]["base_revision"] == 1


@pytest.mark.asyncio
async def test_patch_preserves_immutable_prescription_entry_mode_and_hidden_set_fact(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    body = history_body(
        history_exercise.id,
        base_revision=workout.revision,
        entry_mode="live",
        weight="82.50",
    )

    response = await client.patch(f"/workouts/{workout.id}/history", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["workout"]["status"] == "finished"
    assert response.json()["workout"]["revision"] == 2

    refreshed = (
        await db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == workout.id)
            .options(
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.sets
                )
            )
        )
    ).scalar_one()
    exercise = refreshed.exercises[0]
    assert refreshed.entry_mode == "live"
    assert exercise.prescription == {"target_reps": 8}
    assert exercise.live_prescription == {"target_reps": 9}
    assert exercise.recommended_rir == 2
    assert exercise.sets[0].is_max_reps is True
    assert float(exercise.sets[0].weight) == 82.5


@pytest.mark.asyncio
async def test_patch_can_reorder_exercises_and_sets_with_unique_positions(
    client, history_exercise: Exercise
):
    body = history_body(history_exercise.id)
    body["exercises"][0]["sets"].append(
        {
            **body["exercises"][0]["sets"][0],
            "client_uuid": "history-set-2",
            "set_number": 2,
        }
    )
    body["exercises"].append(
        {
            **body["exercises"][0],
            "client_uuid": "history-exercise-2",
            "order_index": 1,
            "sets": [],
        }
    )
    created = await client.post("/workouts/history", json=body)
    assert created.status_code == 201, created.text
    workout_id = created.json()["workout"]["id"]

    changed = {**body, "base_revision": 0}
    changed["exercises"] = [
        {**body["exercises"][0], "order_index": 1,
         "sets": [
             {**body["exercises"][0]["sets"][0], "set_number": 2},
             {**body["exercises"][0]["sets"][1], "set_number": 1},
         ]},
        {**body["exercises"][1], "order_index": 0},
    ]
    response = await client.patch(f"/workouts/{workout_id}/history", json=changed)

    assert response.status_code == 200, response.text
    exercises = response.json()["workout"]["exercises"]
    assert [(item["client_uuid"], item["order_index"]) for item in exercises] == [
        ("history-exercise-2", 0), ("history-exercise-1", 1)
    ]
    assert [(item["client_uuid"], item["set_number"]) for item in exercises[1]["sets"]] == [
        ("history-set-2", 1), ("history-set-1", 2)
    ]


@pytest.mark.asyncio
async def test_patch_legacy_null_uuids_preserves_immutable_and_hidden_fields(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    loaded = (
        await db.execute(
            select(WorkoutSession)
            .where(WorkoutSession.id == workout.id)
            .options(selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.sets))
        )
    ).scalar_one()
    original_exercise = loaded.exercises[0]
    original_set = original_exercise.sets[0]
    original_exercise.client_uuid = None
    original_set.client_uuid = None
    original_set.superset_round = 1
    original_set.is_anomalous = True
    await db.commit()

    body = history_body(
        history_exercise.id,
        base_revision=workout.revision,
        entry_mode="live",
        weight="82.50",
    )
    body["exercises"][0]["client_uuid"] = f"server-exercise-{original_exercise.id}"
    body["exercises"][0]["sets"][0]["client_uuid"] = f"server-set-{original_set.id}"

    response = await client.patch(f"/workouts/{workout.id}/history", json=body)

    assert response.status_code == 200, response.text
    refreshed = (
        await db.execute(
            select(WorkoutSessionExercise)
            .where(WorkoutSessionExercise.workout_session_id == workout.id)
            .options(selectinload(WorkoutSessionExercise.sets))
        )
    ).scalar_one()
    assert refreshed.id == original_exercise.id
    assert refreshed.prescription == {"target_reps": 8}
    assert refreshed.live_prescription == {"target_reps": 9}
    assert refreshed.sets[0].id == original_set.id
    assert refreshed.sets[0].is_max_reps is True
    assert refreshed.sets[0].superset_round == 1


@pytest.mark.asyncio
async def test_manual_history_runs_anomaly_guard(
    client, db: AsyncSession, history_exercise: Exercise
):
    response = await client.post(
        "/workouts/history",
        json=history_body(history_exercise.id, weight="700.00"),
    )

    assert response.status_code == 201, response.text
    workout_id = response.json()["workout"]["id"]
    workout_set = (
        await db.execute(
            select(WorkoutSessionSet)
            .join(WorkoutSessionExercise)
            .where(WorkoutSessionExercise.workout_session_id == workout_id)
        )
    ).scalar_one()
    assert workout_set.is_anomalous is True


@pytest.mark.asyncio
async def test_patch_rejects_entry_mode_change(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)

    response = await client.patch(
        f"/workouts/{workout.id}/history",
        json=history_body(
            history_exercise.id,
            base_revision=workout.revision,
            entry_mode="manual",
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "immutable_history_field"


@pytest.mark.asyncio
async def test_patch_invalid_exercise_is_atomic(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise
):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    body = history_body(
        2_147_483_000, base_revision=workout.revision, entry_mode="live"
    )

    response = await client.patch(f"/workouts/{workout.id}/history", json=body)

    assert response.status_code == 422, response.text
    await db.refresh(workout)
    assert workout.revision == 1
    assert workout.edited_at is None


@pytest.mark.asyncio
async def test_patch_hides_foreign_workout(
    client, db: AsyncSession, history_exercise: Exercise
):
    other = AppUser(
        firebase_uid=f"foreign-{uuid.uuid4().hex}",
        email=f"foreign-{uuid.uuid4().hex}@example.com",
        display_name="Foreign",
    )
    db.add(other)
    await db.flush()
    workout = WorkoutSession(
        app_user_id=other.id,
        source="free",
        status="finished",
        entry_mode="live",
        revision=1,
        started_at=T0,
        finished_at=T1,
    )
    db.add(workout)
    await db.commit()
    await db.refresh(workout)

    response = await client.patch(
        f"/workouts/{workout.id}/history",
        json=history_body(
            history_exercise.id, base_revision=workout.revision, entry_mode="live"
        ),
    )

    assert response.status_code == 404
    await db.delete(workout)
    await db.delete(other)
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_kind", ["set", "exercise"])
async def test_history_replay_cannot_restore_deleted_legacy_child(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise, child_kind,
):
    from copy import deepcopy
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    loaded = (await db.execute(select(WorkoutSession)
        .where(WorkoutSession.id == workout.id)
        .options(selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.sets)))).scalar_one()
    original_exercise = loaded.exercises[0]
    original_set = original_exercise.sets[0]
    original_exercise.client_uuid = None
    original_set.client_uuid = None
    child_id = original_set.id if child_kind == "set" else original_exercise.id
    await db.commit()

    stale = history_body(history_exercise.id, base_revision=0, entry_mode="live")
    conflict = await client.patch(f"/workouts/{workout.id}/history", json=stale)
    assert conflict.status_code == 409, conflict.text
    retained = conflict.json()["detail"]["server_draft"]
    for item in retained["exercises"]:
        item.pop("exercise_name", None)  # Response-only catalog label.
    assert retained["exercises"][0]["client_uuid"] == f"server-exercise-{original_exercise.id}"
    assert retained["exercises"][0]["sets"][0]["client_uuid"] == f"server-set-{original_set.id}"

    deletion = deepcopy(retained)
    if child_kind == "set":
        deletion["exercises"][0]["sets"] = []
    else:
        deletion["exercises"] = []
    deleted = await client.patch(f"/workouts/{workout.id}/history", json=deletion)
    assert deleted.status_code == 200, deleted.text
    bucket = "sets" if child_kind == "set" else "exercises"
    marker = deleted.json()["workout"]["child_tombstones"][bucket][0]
    assert marker["client_uuid"] is None
    assert marker["server_id"] == child_id

    retained["base_revision"] = deleted.json()["workout"]["revision"]
    replay = await client.patch(f"/workouts/{workout.id}/history", json=retained)
    assert replay.status_code == 200, replay.text
    actual = replay.json()["workout"]
    assert actual["child_tombstones"][bucket] == [marker]
    if child_kind == "set":
        assert actual["exercises"][0]["sets"] == []
    else:
        assert actual["exercises"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("child_kind", ["set", "exercise"])
@pytest.mark.parametrize("case", ["other_workout", "other_type", "named_child", "noncanonical", "oversized", "existing_literal"])
async def test_history_alias_lookup_preserves_unrelated_client_identities(
    client, db: AsyncSession, test_user: AppUser, history_exercise: Exercise, child_kind, case,
):
    from api.services.models import SyncTombstone
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    loaded = (await db.execute(select(WorkoutSession)
        .where(WorkoutSession.id == workout.id)
        .options(selectinload(WorkoutSession.exercises).selectinload(WorkoutSessionExercise.sets)))).scalar_one()
    exercise = loaded.exercises[0]
    row = exercise.sets[0]
    marker_id = 2147480000
    prefix = "server-set-" if child_kind == "set" else "server-exercise-"
    identity = f"{prefix}{marker_id}"
    marker_workout = workout
    marker_type = "workout_set" if child_kind == "set" else "workout_exercise"
    marker_uuid = None
    if case == "other_workout":
        marker_workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    elif case == "other_type":
        marker_type = "workout_exercise" if child_kind == "set" else "workout_set"
    elif case == "named_child":
        marker_uuid = "a-real-different-client-identity"
    elif case == "noncanonical":
        identity = f"{prefix}0{marker_id}"
    elif case == "oversized":
        identity = prefix + "9" * 40
    elif case == "existing_literal":
        (row if child_kind == "set" else exercise).client_uuid = identity
    db.add(SyncTombstone(app_user_id=test_user.id, workout_session_id=marker_workout.id,
        entity_type=marker_type, client_uuid=marker_uuid, server_id=marker_id))
    await db.commit()

    body = history_body(history_exercise.id, base_revision=workout.revision, entry_mode="live")
    body["client_uuid"] = workout.client_uuid
    target = body["exercises"][0]["sets"][0] if child_kind == "set" else body["exercises"][0]
    target["client_uuid"] = identity
    response = await client.patch(f"/workouts/{workout.id}/history", json=body)
    assert response.status_code == 200, response.text
    saved = response.json()["workout"]["exercises"][0]
    if child_kind == "set":
        saved = saved["sets"][0]
    assert saved["client_uuid"] == identity


@pytest.mark.asyncio
async def test_warmup_excluded_from_working_results_but_visible_in_history(client, history_exercise):
    body = history_body(history_exercise.id)
    template = body["exercises"][0]["sets"][0]
    body["exercises"][0]["sets"] = [dict(template, client_uuid=f"warmup-{i}", set_number=i,
        set_type="warmup", weight="200", reps=20) for i in range(1, 4)] + [
        dict(template, client_uuid=f"working-{i}", set_number=i + 3, weight="80", reps=10) for i in range(1, 4)]
    saved = await client.post("/workouts/history", json=body)
    assert saved.status_code == 201, saved.text
    wid = saved.json()["workout"]["id"]
    listing = (await client.get(f"/exercises/{history_exercise.id}/history")).json()
    entry = next(row for row in listing if row["workout_id"] == wid)
    assert (entry["sets_count"], entry["total_reps"], entry["total_volume"]) == (3, 30, 2400)
    detail = (await client.get(f"/exercises/{history_exercise.id}/history/{wid}")).json()
    assert (detail["sets_count"], detail["total_reps"], detail["total_volume"]) == (3, 30, 2400)
    assert len([s for s in detail["sets"] if s["set_type"] == "warmup"]) == 3
    last = (await client.get(f"/exercises/{history_exercise.id}/last-performance")).json()
    assert [s["set_number"] for s in last["sets"]] == [4, 5, 6]


@pytest.mark.asyncio
async def test_history_preserves_occurrence_provenance_and_factual_load_context(client, history_exercise):
    from copy import deepcopy
    from tests.integration.test_sync_warmup_snapshots import group_fixture, row_fixture
    body = history_body(history_exercise.id)
    first = body["exercises"][0]
    group = group_fixture()
    first["warmup_group"] = group
    row = first["sets"][0]
    row.update(set_type="warmup", warmup_snapshot=row_fixture(group), load_mode="stack",
        gym_profile_id=str(uuid.uuid4()), setup_id=str(uuid.uuid4()),
        load_snapshot={"weight_basis": "displayed", "display_unit": "lbs", "equipment": group["versions"][0]["equipment"]})
    second = deepcopy(first)
    second.update(client_uuid="history-exercise-2", order_index=1, warmup_group=group_fixture())
    second["sets"][0].update(client_uuid="warmup-second", warmup_snapshot=row_fixture(second["warmup_group"]))
    legacy = deepcopy(first)
    legacy.update(client_uuid="history-exercise-3", order_index=2)
    legacy.pop("warmup_group")
    legacy["sets"][0].update(client_uuid="legacy-warmup")
    legacy["sets"][0].pop("warmup_snapshot")
    body["exercises"] += [second, legacy]
    created = await client.post("/workouts/history", json=body)
    assert created.status_code == 201, created.text
    workout = created.json()["workout"]
    wid = workout["id"]
    saved = workout["exercises"][0]["sets"][0]
    assert saved["load_snapshot"] == row["load_snapshot"]
    detail = (await client.get(f"/exercises/{history_exercise.id}/history/{wid}")).json()
    assert len(detail["warmup_groups"]) == 2
    for occurrence, group_row in zip(workout["exercises"][:2], detail["warmup_groups"]):
        assert group_row == {"workout_session_exercise_id": occurrence["id"], "client_uuid": occurrence["client_uuid"], "warmup_group": occurrence["warmup_group"]}
        fact = next(s for s in detail["sets"] if s["workout_session_exercise_id"] == occurrence["id"])
        assert fact["client_uuid"] == occurrence["sets"][0]["client_uuid"]
        assert fact["warmup_snapshot"] == occurrence["sets"][0]["warmup_snapshot"]
    assert next(s for s in detail["sets"] if s["client_uuid"] == "legacy-warmup")["warmup_snapshot"] is None
    original_group = deepcopy(workout["exercises"][0]["warmup_group"])
    for item in body["exercises"]:
        item.pop("warmup_group", None)
        for fact in item["sets"]:
            for key in ("warmup_snapshot", "load_mode", "gym_profile_id", "setup_id", "load_snapshot"):
                fact.pop(key, None)
    body["notes"] = "old client unrelated correction"
    corrected = await client.patch(f"/workouts/{wid}/history", json=body)
    assert corrected.status_code == 200, corrected.text
    current = corrected.json()["workout"]
    assert current["exercises"][0]["warmup_group"] == original_group
    assert current["exercises"][0]["sets"][0]["load_snapshot"] == saved["load_snapshot"]
    assert current["exercises"][0]["sets"][0]["warmup_snapshot"] == saved["warmup_snapshot"]
    retry = await client.patch(f"/workouts/{wid}/history", json=body)
    assert retry.status_code == 200, retry.text
    conflict = await client.patch(f"/workouts/{wid}/history", json={**body, "notes": "stale edit"})
    assert conflict.status_code == 409, conflict.text
    server = conflict.json()["detail"]["server_draft"]
    assert server["exercises"][0]["sets"][0]["load_snapshot"] == saved["load_snapshot"]
    body["base_revision"] = current["revision"]
    for key in ("load_mode", "gym_profile_id", "setup_id", "load_snapshot"):
        body["exercises"][0]["sets"][0][key] = None
    cleared = await client.patch(f"/workouts/{wid}/history", json=body)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["workout"]["exercises"][0]["sets"][0]["load_snapshot"] is None
    assert cleared.json()["workout"]["exercises"][0]["warmup_group"] == original_group


@pytest.mark.asyncio
async def test_finish_response_excludes_warmup_preserves_drop(client, db, test_user, history_exercise):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    workout.status = "active"
    workout.finished_at = None
    ex = (await db.execute(select(WorkoutSessionExercise).where(WorkoutSessionExercise.workout_session_id == workout.id))).scalar_one()
    db.add_all([WorkoutSessionSet(workout_session_exercise_id=ex.id, set_number=2, set_type="warmup", weight=200, reps=20, is_completed=True),
        WorkoutSessionSet(workout_session_exercise_id=ex.id, set_number=3, set_type="drop", weight=40, reps=5, is_completed=True)])
    await db.commit()
    response = await client.post(f"/workouts/{workout.id}/finish")
    assert response.status_code == 200, response.text
    assert response.json()["sets_count"] == 2
    assert response.json()["total_reps"] == 13
    assert float(response.json()["total_volume"]) == 840


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", [False, True])
async def test_last_performance_skips_newer_warmup_only_session(client, history_exercise, variant):
    from copy import deepcopy
    body = history_body(history_exercise.id)
    body["exercises"][0]["sets"][0].update(load_mode="stack", load_snapshot={"weight_basis": "displayed"})
    old = await client.post("/workouts/history", json=body)
    assert old.status_code == 201, old.text
    recent = deepcopy(body)
    recent.update(client_uuid="recent-warmup-only", started_at=(T0 + timedelta(days=1)).isoformat(), finished_at=(T1 + timedelta(days=1)).isoformat())
    recent["exercises"][0]["sets"][0].update(set_type="warmup", weight="200", reps=20)
    saved = await client.post("/workouts/history", json=recent)
    assert saved.status_code == 201, saved.text
    query = "?load_mode=stack&weight_basis=displayed" if variant else ""
    last = await client.get(f"/exercises/{history_exercise.id}/last-performance{query}")
    assert last.status_code == 200, last.text
    assert last.json()["workout_id"] == old.json()["workout"]["id"]
    assert [s["set_type"] for s in last.json()["sets"]] == ["normal"]


@pytest.mark.asyncio
async def test_fatigue_history_volume_excludes_warmups(client, db, test_user, history_exercise):
    workout = await _create_finished_workout(db, test_user.id, history_exercise.id)
    workout.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    workout.finished_at = datetime.now(timezone.utc)
    history_exercise.secondary_muscle_groups = ["triceps"]
    ex = (await db.execute(select(WorkoutSessionExercise).where(WorkoutSessionExercise.workout_session_id == workout.id))).scalar_one()
    db.add(WorkoutSessionSet(workout_session_exercise_id=ex.id, set_number=2, set_type="drop", weight=40, reps=5, is_completed=True))
    await db.commit()
    before = {}
    for muscle in ("chest", "triceps"):
        result = await client.get(f"/progress/fatigue/{muscle}")
        assert result.status_code == 200, result.text
        before[muscle] = result.json()
    for i in range(3, 6):
        db.add(WorkoutSessionSet(workout_session_exercise_id=ex.id, set_number=i, set_type="warmup", weight=200, reps=20, is_completed=True))
    await db.commit()
    for muscle in ("chest", "triceps"):
        after = await client.get(f"/progress/fatigue/{muscle}")
        assert after.status_code == 200, after.text
        assert after.json() == before[muscle]
    assert sum(row["direct_volume"] for row in before["chest"]["history"]) == 2
    assert sum(row["indirect_volume"] for row in before["triceps"]["history"]) == 1
