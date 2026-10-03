"""HTTP contract tests for owner gym profiles, exercise setups, and load preferences."""

import uuid

import pytest
from sqlalchemy import select

from api.services.models import AppUser, Exercise


def gym_payload(name="Garage", revision=0):
    return {
        "name": name,
        "equipment": ["barbell"],
        "bars": [{"weight": 20, "count": 1, "unit": "kg"}],
        "discs": [],
        "steps": [],
        "expected_revision": revision,
    }


def setup_payload(mode, *, setup_id=None, revision=0, basis=None):
    return {
        "id": str(setup_id or uuid.uuid4()),
        "is_available": True,
        "is_preferred": mode == "stack",
        "step_value": 2.5,
        "step_unit": "kg",
        "loading_sides": 1,
        "base_weight": None,
        "weight_basis": basis or ("displayed" if mode == "stack" else "plates_only"),
        "plate_inventory": None,
        "expected_revision": revision,
    }


def preference_payload(*, preference_id=None, revision=0, enabled=None, preferred="stack"):
    return {
        "id": str(preference_id or uuid.uuid4()),
        "enabled_modes": enabled or ["stack", "plate_loaded"],
        "preferred_mode": preferred,
        "expected_revision": revision,
    }


async def make_exercise(db, *, owner_id=None, name=None):
    row = Exercise(
        name=name or f"API exercise {uuid.uuid4()}",
        category="strength",
        main_muscle_group="chest",
        secondary_muscle_groups=[],
        equipment_needed=[],
        difficulty="beginner",
        source="test",
        app_user_id=owner_id,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def make_other_user(db):
    row = AppUser(
        firebase_uid=f"other-{uuid.uuid4()}",
        email=f"other-{uuid.uuid4()}@example.com",
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_gym_profile_http_lifecycle_and_active_flag(client, auth_headers):
    gym_id = str(uuid.uuid4())
    payload = gym_payload()

    created = await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers, json=payload)
    assert created.status_code == 200, created.text
    assert created.json()["id"] == gym_id
    assert created.json()["revision"] == 1
    assert created.json()["is_active"] is False
    assert isinstance(created.json()["bars"][0]["weight"], (int, float))

    retried = await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers, json=payload)
    assert retried.status_code == 200 and retried.json()["revision"] == 1

    selected = await client.put(
        "/gym-profiles/active", headers=auth_headers,
        json={"gym_profile_id": gym_id},
    )
    assert selected.status_code == 200 and selected.json() == {"gym_profile_id": gym_id}
    listed = await client.get("/gym-profiles", headers=auth_headers)
    assert listed.status_code == 200 and listed.json()[0]["is_active"] is True

    changed = await client.put(
        f"/gym-profiles/{gym_id}", headers=auth_headers,
        json=gym_payload("Garage Plus", 1),
    )
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    stale = await client.put(
        f"/gym-profiles/{gym_id}", headers=auth_headers,
        json=gym_payload("Old edit", 1),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current"]["revision"] == 2
    assert stale.json()["detail"]["current"]["name"] == "Garage Plus"

    deleted = await client.request(
        "DELETE", f"/gym-profiles/{gym_id}", headers=auth_headers,
        json={"expected_revision": 2},
    )
    assert deleted.status_code == 200 and deleted.json()["revision"] == 3
    assert deleted.json()["is_active"] is False
    retried_delete = await client.request(
        "DELETE", f"/gym-profiles/{gym_id}", headers=auth_headers,
        json={"expected_revision": 2},
    )
    assert retried_delete.status_code == 200 and retried_delete.json()["revision"] == 3
    assert (await client.get("/gym-profiles", headers=auth_headers)).json() == []


@pytest.mark.asyncio
async def test_gym_owner_isolation_deleted_and_foreign_are_not_found(client, auth_headers, db, test_user):
    foreign_owner = await make_other_user(db)
    foreign_id = str(uuid.uuid4())
    from api.services.gym_profiles import put_gym_profile
    from api.schemas.gym_profiles import GymProfilePayload
    await put_gym_profile(db, foreign_owner.id, uuid.UUID(foreign_id), GymProfilePayload(**gym_payload()))

    hidden = await client.put(
        f"/gym-profiles/{foreign_id}", headers=auth_headers,
        json=gym_payload("Hijack", 1),
    )
    assert hidden.status_code == 404
    assert foreign_id not in hidden.text

    own_id = str(uuid.uuid4())
    own = await client.put(
        f"/gym-profiles/{own_id}", headers=auth_headers, json=gym_payload()
    )
    assert own.status_code == 200
    deleted = await client.request(
        "DELETE", f"/gym-profiles/{own_id}", headers=auth_headers,
        json={"expected_revision": 1},
    )
    assert deleted.status_code == 200
    unavailable = await client.get(
        f"/gym-profiles/{own_id}/exercise-setups", headers=auth_headers
    )
    assert unavailable.status_code == 404


@pytest.mark.asyncio
async def test_setup_modes_get_validation_delete_retry_and_replacement(
    client, auth_headers, db
):
    gym_id, global_ex = str(uuid.uuid4()), await make_exercise(db)
    created_gym = await client.put(
        f"/gym-profiles/{gym_id}", headers=auth_headers, json=gym_payload()
    )
    assert created_gym.status_code == 200

    responses = {}
    for mode in ("stack", "plate_loaded"):
        response = await client.put(
            f"/gym-profiles/{gym_id}/exercise-setups/global/{global_ex.id}/{mode}",
            headers=auth_headers, json=setup_payload(mode),
        )
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == mode
        responses[mode] = response.json()

    listed = await client.get(
        f"/gym-profiles/{gym_id}/exercise-setups", headers=auth_headers
    )
    assert listed.status_code == 200
    assert {row["mode"] for row in listed.json()} == {"stack", "plate_loaded"}
    assert listed.json()[0]["step_value"] == 2.5

    invalid_mode = await client.put(
        f"/gym-profiles/{gym_id}/exercise-setups/global/{global_ex.id}/bad-mode",
        headers=auth_headers, json=setup_payload("stack"),
    )
    assert invalid_mode.status_code == 422
    invalid_basis = await client.put(
        f"/gym-profiles/{gym_id}/exercise-setups/global/{global_ex.id}/stack",
        headers=auth_headers, json=setup_payload("stack", basis="plates_only"),
    )
    assert invalid_basis.status_code == 422

    setup_id = responses["stack"]["id"]
    deleted = await client.request(
        "DELETE",
        f"/gym-profiles/{gym_id}/exercise-setups/global/{global_ex.id}/stack",
        headers=auth_headers, json={"id": setup_id, "expected_revision": 1},
    )
    assert deleted.status_code == 200 and deleted.json()["revision"] == 2
    retry = await client.request(
        "DELETE",
        f"/gym-profiles/{gym_id}/exercise-setups/global/{global_ex.id}/stack",
        headers=auth_headers, json={"id": setup_id, "expected_revision": 1},
    )
    assert retry.status_code == 200 and retry.json()["id"] == setup_id
    replacement = await client.put(
        f"/gym-profiles/{gym_id}/exercise-setups/global/{global_ex.id}/stack",
        headers=auth_headers, json=setup_payload("stack"),
    )
    assert replacement.status_code == 200 and replacement.json()["id"] != setup_id
    assert replacement.json()["revision"] == 1


@pytest.mark.asyncio
async def test_setup_owner_exercise_scope_and_conflict_current_row(client, auth_headers, db, test_user):
    gym_id = str(uuid.uuid4())
    assert (await client.put(f"/gym-profiles/{gym_id}", headers=auth_headers, json=gym_payload())).status_code == 200
    own = await make_exercise(db, owner_id=test_user.id)
    foreign_owner = await make_other_user(db)
    foreign = await make_exercise(db, owner_id=foreign_owner.id)
    for source, exercise in (("user", foreign), ("global", own)):
        response = await client.put(
            f"/gym-profiles/{gym_id}/exercise-setups/{source}/{exercise.id}/stack",
            headers=auth_headers, json=setup_payload("stack"),
        )
        assert response.status_code == 404

    global_ex = await make_exercise(db)
    path = f"/gym-profiles/{gym_id}/exercise-setups/global/{global_ex.id}/stack"
    created = await client.put(path, headers=auth_headers, json=setup_payload("stack"))
    assert created.status_code == 200
    conflict = await client.put(
        path, headers=auth_headers, json={**setup_payload("stack"), "is_available": False},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current"]["id"] == created.json()["id"]
    assert conflict.json()["detail"]["current"]["revision"] == 1


@pytest.mark.asyncio
async def test_preference_get_create_retry_stale_conflict_and_exercise_ownership(
    client, auth_headers, db, test_user
):
    global_ex = await make_exercise(db)
    path = f"/exercise-load-preferences/global/{global_ex.id}"
    missing = await client.get(path, headers=auth_headers)
    assert missing.status_code == 200 and missing.json() is None

    payload = preference_payload()
    created = await client.put(path, headers=auth_headers, json=payload)
    assert created.status_code == 200 and created.json()["revision"] == 1
    assert (await client.get(path, headers=auth_headers)).json()["id"] == payload["id"]
    retried = await client.put(path, headers=auth_headers, json=payload)
    assert retried.status_code == 200 and retried.json()["revision"] == 1

    stale = await client.put(
        path, headers=auth_headers,
        json=preference_payload(preference_id=payload["id"], revision=0, enabled=["stack"], preferred="stack"),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current"]["revision"] == 1
    assert stale.json()["detail"]["current"]["id"] == payload["id"]

    own = await make_exercise(db, owner_id=test_user.id)
    foreign_owner = await make_other_user(db)
    foreign = await make_exercise(db, owner_id=foreign_owner.id)
    assert (await client.get(f"/exercise-load-preferences/user/{own.id}", headers=auth_headers)).status_code == 200
    assert (await client.get(f"/exercise-load-preferences/user/{foreign.id}", headers=auth_headers)).status_code == 404
    assert (await client.get(f"/exercise-load-preferences/global/{own.id}", headers=auth_headers)).status_code == 404
