"""Snapshot compatibility, provenance and deletion regression tests."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest
from tests.integration.test_sync_reliability import _exercise_id, _snapshot

pytestmark = pytest.mark.asyncio


def group_fixture():
    context = {"kind": "step", "gym_profile_id": None, "setup_id": None,
        "load_mode": None, "loading_sides": 1, "weight_basis": "displayed",
        "base_weight_kg": None, "bar": None, "collar": None,
        "step": {"value": 2.5, "unit": "kg"},
        "inventory": {"mode": "unlimited", "items": []}}
    settings = {"count": 3, "between_seconds": 0, "before_work": {"mode": "working"},
        "category": "unknown", "count_origin": "preview", "warnings": ["unknown_category"]}
    version = str(uuid4())
    return {"id": str(uuid4()), "active_version": version,
        "updated_at": "2026-10-09T00:00:00Z", "versions": [{"version": version,
        "calculated_at": "2026-10-09T00:00:00Z", "algorithm_version": 1,
        "source": {"set_client_uuid": str(uuid4()), "weight_kg": 80, "reps": 8,
            "rir": 2, "load_context": context}, "source_e1rm_kg": 106.6666667,
        "settings": settings, "equipment": context}]}


def row_fixture(group, order=1):
    return {"group_id": group["id"], "calculation_version": group["active_version"],
        "order": order, "fraction": order / 4, "raw_weight_kg": 20 * order,
        "manually_edited": False}


async def payload_fixture(client):
    payload = _snapshot(str(uuid4()), await _exercise_id(client))
    exercise = payload["exercises"][0]
    group = group_fixture()
    exercise["warmup_group"] = group
    exercise["sets"] = [{"client_uuid": str(uuid4()), "set_number": i,
        "set_type": "warmup", "weight": i * 20, "reps": 10,
        "warmup_snapshot": row_fixture(group, i)} for i in range(1, 4)]
    return payload


async def push(client, payload):
    response = await client.post("/sync/workouts", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["workout"]


async def test_sync_retry_preserves_uuids_and_group(client):
    payload = await payload_fixture(client)
    first = await push(client, payload)
    second = await push(client, payload)
    actual = second["exercises"][0]
    assert len(actual["sets"]) == 3
    assert {s["client_uuid"] for s in actual["sets"]} == {s["client_uuid"] for s in payload["exercises"][0]["sets"]}
    assert actual["warmup_group"] == payload["exercises"][0]["warmup_group"]
    assert [s["id"] for s in actual["sets"]] == [s["id"] for s in first["exercises"][0]["sets"]]
    # Reserved source identity is valid before the working row exists.
    source = actual["warmup_group"]["versions"][0]["source"]["set_client_uuid"]
    payload["exercises"][0]["sets"].append({"client_uuid": source, "set_number": 4,
        "weight": 80, "reps": 8})
    later = await push(client, payload)
    assert later["exercises"][0]["sets"][-1]["client_uuid"] == source
    detail = await client.get(f"/workouts/{first['id']}")
    assert detail.json()["exercises"][0]["warmup_group"] == actual["warmup_group"]
    changes = await client.get("/sync/changes")
    found = next(w for w in changes.json()["workouts"] if w["id"] == first["id"])
    assert found["exercises"][0]["sets"][0]["warmup_snapshot"] == row_fixture(actual["warmup_group"])


async def test_missing_fields_preserve_but_explicit_null_clears(client):
    payload = await payload_fixture(client)
    saved = (await push(client, payload))["exercises"][0]
    assert saved["warmup_group"] == payload["exercises"][0]["warmup_group"]
    exercise = payload["exercises"][0]
    del exercise["warmup_group"]
    for row in exercise["sets"]:
        del row["warmup_snapshot"]
    legacy = (await push(client, payload))["exercises"][0]
    assert legacy["warmup_group"] == saved["warmup_group"]
    assert legacy["sets"][0]["warmup_snapshot"] == saved["sets"][0]["warmup_snapshot"]
    exercise["warmup_group"] = None
    for row in exercise["sets"]:
        row["warmup_snapshot"] = None
    cleared = (await push(client, payload))["exercises"][0]
    assert cleared["warmup_group"] is None
    assert all(row["warmup_snapshot"] is None for row in cleared["sets"])


@pytest.mark.parametrize("case", ["version", "group", "duplicate_order", "negative_weight", "too_many_reps"])
async def test_snapshot_rejects_invalid_refs(client, case):
    payload = await payload_fixture(client)
    await push(client, payload)
    exercise = payload["exercises"][0]
    del exercise["warmup_group"]  # Validate against saved group too.
    row = exercise["sets"][0]
    if case == "version": row["warmup_snapshot"]["calculation_version"] = str(uuid4())
    if case == "group": row["warmup_snapshot"]["group_id"] = str(uuid4())
    if case == "duplicate_order": exercise["sets"][1]["warmup_snapshot"]["order"] = 1
    if case == "negative_weight": row["weight"] = -1
    if case == "too_many_reps": row["reps"] = 1001
    response = await client.post("/sync/workouts", json=payload)
    assert response.status_code == 422, response.text


async def test_manual_group_and_legacy_warmups(client):
    payload = await payload_fixture(client)
    exercise = payload["exercises"][0]
    calculated = exercise["warmup_group"]["versions"][0]
    exercise["warmup_group"].update(active_version=None, versions=[],
        settings=calculated["settings"], equipment=calculated["equipment"])
    for row in exercise["sets"]:
        row["warmup_snapshot"].update(calculation_version=None, fraction=None,
            raw_weight_kg=None, manually_edited=True)
    actual = (await push(client, payload))["exercises"][0]
    assert actual["warmup_group"]["versions"] == []
    assert actual["sets"][0]["warmup_snapshot"]["calculation_version"] is None
    legacy = _snapshot(str(uuid4()), exercise["exercise_id"])
    legacy["exercises"][0]["sets"][0]["set_type"] = "warmup"
    assert (await push(client, legacy))["exercises"][0]["warmup_group"] is None


@pytest.mark.parametrize("via_crud", [False, True])
async def test_tombstone_and_old_snapshot_do_not_resurrect_rows(client, via_crud):
    payload = await payload_fixture(client)
    initial = await push(client, payload)
    deleted_uuid = payload["exercises"][0]["sets"][0]["client_uuid"]
    if via_crud:
        response = await client.delete(f"/workout-session-sets/{initial['exercises'][0]['sets'][0]['id']}")
        assert response.status_code == 204
    else:
        deleted = deepcopy(payload)
        deleted["exercises"][0]["sets"][0]["deleted"] = True
        await push(client, deleted)
        await push(client, deleted)
    actual = (await push(client, payload))["exercises"][0]
    assert len(actual["sets"]) == 2
    assert deleted_uuid not in {row["client_uuid"] for row in actual["sets"]}


async def test_crud_row_preserves_and_clears_snapshot(client):
    payload = await payload_fixture(client)
    workout = await push(client, payload)
    exercise = workout["exercises"][0]
    row = exercise["sets"][0]
    url = f"/workout-session-sets/{row['id']}"
    changed = await client.patch(url, json={"reps": 11})
    assert changed.status_code == 200, changed.text
    assert changed.json()["warmup_snapshot"] == row["warmup_snapshot"]
    invalid = deepcopy(row["warmup_snapshot"])
    invalid["calculation_version"] = str(uuid4())
    assert (await client.patch(url, json={"warmup_snapshot": invalid})).status_code == 422
    manual = row_fixture(exercise["warmup_group"], 4)
    manual.update(calculation_version=None, fraction=None, raw_weight_kg=None, manually_edited=True)
    created = await client.post(f"/workout-session-exercises/{exercise['id']}/sets",
        json={"set_type": "warmup", "warmup_snapshot": manual, "weight": 0, "reps": 0})
    assert created.status_code == 200, created.text
    assert created.json()["warmup_snapshot"] == manual
    cleared = await client.patch(url, json={"warmup_snapshot": None})
    assert cleared.status_code == 200
    assert cleared.json()["warmup_snapshot"] is None


async def test_history_preserves_snapshot_fields_and_explicit_clear(client):
    payload = await payload_fixture(client)
    now = datetime.now(timezone.utc)
    body = {"client_uuid": payload["client_uuid"], "base_revision": 0, "entry_mode": "manual",
        "started_at": (now - timedelta(hours=1)).isoformat(), "finished_at": now.isoformat(),
        "exercises": payload["exercises"]}
    created = await client.post("/workouts/history", json=body)
    assert created.status_code in (200, 201), created.text
    workout = created.json()["workout"]
    saved = workout["exercises"][0]
    assert saved["warmup_group"] == payload["exercises"][0]["warmup_group"]
    body["base_revision"] = workout["revision"]
    del body["exercises"][0]["warmup_group"]
    for row in body["exercises"][0]["sets"]: del row["warmup_snapshot"]
    body["notes"] = "legacy correction"
    changed = await client.patch(f"/workouts/{workout['id']}/history", json=body)
    assert changed.status_code == 200, changed.text
    assert changed.json()["workout"]["exercises"][0]["warmup_group"] == saved["warmup_group"]
    assert changed.json()["workout"]["exercises"][0]["sets"][0]["warmup_snapshot"] == saved["sets"][0]["warmup_snapshot"]
    retry = await client.patch(f"/workouts/{workout['id']}/history", json=body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["workout"]["revision"] == changed.json()["workout"]["revision"]
    # Conflict draft must retain snapshots too.
    conflict = await client.patch(f"/workouts/{workout['id']}/history", json={**body, "notes": "stale"})
    assert conflict.status_code == 409, conflict.text
    assert "warmup_group" in str(conflict.json())
    body["base_revision"] = changed.json()["workout"]["revision"]
    body["exercises"][0]["warmup_group"] = None
    for row in body["exercises"][0]["sets"]: row["warmup_snapshot"] = None
    cleared = await client.patch(f"/workouts/{workout['id']}/history", json=body)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["workout"]["exercises"][0]["warmup_group"] is None


async def test_repeat_generated_row_has_manual_provenance(client):
    workout = await push(client, await payload_fixture(client))
    exercise = workout["exercises"][0]
    repeated = await client.post(f"/workout-session-sets/{exercise['sets'][0]['id']}/repeat", json={})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["warmup_snapshot"] == {
        "group_id": exercise["warmup_group"]["id"], "calculation_version": None,
        "order": 4, "fraction": None, "raw_weight_kg": None, "manually_edited": True}


async def test_child_tombstones_are_scoped_and_pulled(client):
    payload = await payload_fixture(client)
    workout = await push(client, payload)
    row = workout["exercises"][0]["sets"][0]
    since = datetime.now(timezone.utc).isoformat()
    assert (await client.delete(f"/workout-session-sets/{row['id']}")).status_code == 204
    changes = (await client.get("/sync/changes", params={"since": since})).json()
    changed = next(w for w in changes["workouts"] if w["id"] == workout["id"])
    marker = changed["child_tombstones"]["sets"][0]
    assert marker["client_uuid"] == row["client_uuid"]
    assert marker["server_id"] == row["id"]
    assert marker["deleted_at"]
    assert all(t["client_uuid"] != row["client_uuid"] for t in changes["tombstones"])
    other = await payload_fixture(client)
    other["exercises"][0]["sets"][0]["client_uuid"] = row["client_uuid"]
    other_workout = await push(client, other)
    assert len(other_workout["exercises"][0]["sets"]) == 3
    assert other_workout["child_tombstones"] == {"sets": [], "exercises": []}
    # Fresh version also cannot revive a deliberately removed identity.
    payload["base_version"] = changes["versions"][str(workout["id"]) ]
    assert len((await push(client, payload))["exercises"][0]["sets"]) == 2


async def test_parent_tombstone_prevents_old_snapshot_recreation(client, db, test_user):
    from sqlalchemy import select, func
    from api.services.models import SyncTombstone
    payload = await payload_fixture(client)
    workout = await push(client, payload)
    await client.delete(f"/workout-session-sets/{workout['exercises'][0]['sets'][0]['id']}")
    deleted = await client.post("/sync/workouts", json={**payload, "deleted": True})
    assert deleted.status_code == 200, deleted.text
    replay = await client.post("/sync/workouts", json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["deleted"] is True
    assert replay.json()["workout"] is None
    assert await db.scalar(select(func.count()).select_from(SyncTombstone).where(
        SyncTombstone.app_user_id == test_user.id,
        SyncTombstone.entity_type == "workout_set")) == 0


async def test_legacy_null_uuid_child_marker_uses_server_id(client):
    payload = await payload_fixture(client)
    workout = await push(client, payload)
    exercise = workout["exercises"][0]
    created = await client.post(f"/workout-session-exercises/{exercise['id']}/sets",
        json={"set_type": "warmup", "weight": 15, "reps": 10})
    assert created.status_code == 200, created.text
    row_id = created.json()["id"]
    assert (await client.delete(f"/workout-session-sets/{row_id}")).status_code == 204
    detail = (await client.get(f"/workouts/{workout['id']}")).json()
    marker = detail["child_tombstones"]["sets"][0]
    assert marker["client_uuid"] is None
    assert marker["server_id"] == row_id
    payload["exercises"][0]["sets"].append({"client_uuid": str(uuid4()), "server_id": row_id,
        "set_number": 4, "set_type": "warmup"})
    assert len((await push(client, payload))["exercises"][0]["sets"]) == 3


@pytest.mark.parametrize("via_crud", [False, True])
async def test_deleted_exercise_retry_stays_deleted(client, via_crud):
    payload = await payload_fixture(client)
    workout = await push(client, payload)
    if via_crud:
        deleted = await client.delete(f"/workout-session-exercises/{workout['exercises'][0]['id']}")
        assert deleted.status_code == 204, deleted.text
    else:
        deletion = deepcopy(payload)
        deletion["exercises"][0]["deleted"] = True
        await push(client, deletion)
    replay = await push(client, payload)
    assert replay["exercises"] == []
    assert replay["child_tombstones"]["exercises"][0]["client_uuid"] == payload["exercises"][0]["client_uuid"]


async def test_snapshot_validation_is_atomic_and_partial_orders_remain_unique(client):
    payload = await payload_fixture(client)
    workout = await push(client, payload)
    partial = deepcopy(payload)
    partial["exercises"][0]["sets"] = [partial["exercises"][0]["sets"][0]]
    partial["exercises"][0]["sets"][0]["warmup_snapshot"]["order"] = 2
    partial["exercises"][0]["warmup_group"]["updated_at"] = "2026-10-09T01:00:00Z"
    response = await client.post("/sync/workouts", json=partial)
    assert response.status_code == 422, response.text
    detail = (await client.get(f"/workouts/{workout['id']}")).json()
    assert detail["exercises"][0]["warmup_group"] == workout["exercises"][0]["warmup_group"]
    assert detail["exercises"][0]["sets"][0]["warmup_snapshot"]["order"] == 1
