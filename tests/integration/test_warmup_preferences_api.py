"""Real PostgreSQL API coverage for warmup defaults and owned preferences."""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete

from api.services.models import AppUser, AppUserProfile, Exercise

DEFAULTS = {"compound_count": 3, "isolation_count": 2, "between_seconds": 30,
            "before_work": {"mode": "working"}}


@pytest_asyncio.fixture
async def warmup_profile(db, test_user):
    profile = AppUserProfile(app_user_id=test_user.id, settings={"language": "en"})
    db.add(profile)
    await db.commit()
    return profile


@pytest_asyncio.fixture
async def warmup_exercises(db, test_user):
    other = AppUser(firebase_uid=f"warmup-other-{uuid.uuid4()}", email=f"warmup-{uuid.uuid4()}@example.com")
    db.add(other)
    await db.flush()
    rows = [Exercise(name=f"warmup-{uuid.uuid4()}", category="base", main_muscle_group="chest",
                     difficulty="beginner", source="test", app_user_id=owner)
            for owner in (None, test_user.id, other.id)]
    db.add_all(rows)
    await db.commit()
    yield rows
    await db.execute(delete(Exercise).where(Exercise.id.in_([row.id for row in rows])))
    await db.execute(delete(AppUser).where(AppUser.id == other.id))
    await db.commit()


def preference(override, *, row_id=None, revision=0):
    return {"id": row_id or str(uuid.uuid4()), "override": override, "expected_revision": revision}


@pytest.mark.asyncio
async def test_defaults_and_partial_update(client, auth_headers, warmup_profile, db):
    profile = (await client.get("/profile", headers=auth_headers)).json()
    assert profile["settings"]["warmup"] == DEFAULTS
    await db.refresh(warmup_profile)
    assert "warmup" not in warmup_profile.settings
    first = await client.patch("/profile/settings", headers=auth_headers,
                               json={"warmup": {"compound_count": 4, "between_seconds": 0,
                                                "before_work": {"mode": "seconds", "seconds": 0}}})
    assert first.status_code == 200, first.text
    second = await client.patch("/profile/settings", headers=auth_headers,
                                json={"warmup": {"isolation_count": 5}})
    assert second.json()["settings"]["warmup"] == {
        "compound_count": 4, "isolation_count": 5, "between_seconds": 0,
        "before_work": {"mode": "seconds", "seconds": 0},
    }
    assert (await client.get("/profile", headers=auth_headers)).json()["settings"] == second.json()["settings"]


@pytest.mark.asyncio
async def test_owned_source_key_and_reset(client, auth_headers, warmup_exercises):
    global_ex, owned_ex, foreign_ex = warmup_exercises
    global_put = await client.put(f"/warmup-preferences/global/{global_ex.id}", headers=auth_headers,
                                  json=preference({"count": 4, "between_seconds": 0, "before_work": {"mode": "seconds", "seconds": 0}}))
    assert global_put.status_code == 200, global_put.text
    global_row = global_put.json()
    user_put = await client.put(f"/warmup-preferences/user/{owned_ex.id}", headers=auth_headers,
                                json=preference({"count": 2}))
    assert user_put.status_code == 200, user_put.text
    user_row = user_put.json()
    assert global_row["override"]["count"] == 4
    assert user_row["override"]["count"] == 2
    reset = await client.put(f"/warmup-preferences/global/{global_ex.id}", headers=auth_headers,
                             json=preference({"count": None}, row_id=global_row["id"], revision=1))
    assert reset.status_code == 200, reset.text
    reset_row = reset.json()
    assert reset_row["override"]["count"] is None
    assert reset_row["override"]["between_seconds"] == 0
    assert reset_row["override"]["before_work"] == {"mode": "seconds", "seconds": 0}
    for source, ex_id in (("user", foreign_ex.id), ("global", owned_ex.id), ("user", global_ex.id)):
        forbidden = await client.put(f"/warmup-preferences/{source}/{ex_id}", headers=auth_headers,
                                     json=preference({"count": 2}))
        assert forbidden.status_code == 404
    rows = (await client.get("/warmup-preferences", headers=auth_headers)).json()
    assert {(r["exercise_source"], r["exercise_id"]) for r in rows} == {("global", global_ex.id), ("user", owned_ex.id)}


@pytest.mark.asyncio
async def test_old_settings_do_not_erase_warmup(client, auth_headers, warmup_profile):
    saved = await client.patch("/profile/settings", headers=auth_headers,
                               json={"warmup": {"compound_count": 4, "isolation_count": 3}})
    saved_warmup = saved.json()["settings"]["warmup"]
    legacy = await client.patch("/profile/settings", headers=auth_headers, json={"language": "ru", "weight_unit": "lbs"})
    assert legacy.status_code == 200, legacy.text
    after_legacy_patch = legacy.json()["settings"]
    assert after_legacy_patch["warmup"] == saved_warmup
    assert after_legacy_patch["language"] == "ru"


@pytest.mark.asyncio
async def test_revision_retry(client, auth_headers, warmup_exercises):
    ex = warmup_exercises[0]
    path = f"/warmup-preferences/global/{ex.id}"
    payload = preference({"count": 4})
    initial = await client.put(path, headers=auth_headers, json=payload)
    assert initial.status_code == 200, initial.text
    retry = await client.put(path, headers=auth_headers, json=payload)
    assert retry.json() == initial.json()
    change = preference({"count": 2}, row_id=payload["id"], revision=1)
    changed = await client.put(path, headers=auth_headers, json=change)
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    assert (await client.put(path, headers=auth_headers, json=change)).json() == changed.json()
    stale = await client.put(path, headers=auth_headers, json=payload)
    assert stale.status_code == 409
    assert stale.json()["detail"]["current"] == changed.json()
    duplicate = await client.put(path, headers=auth_headers, json=preference({"count": 2}))
    assert duplicate.status_code == 409
    foreign_key = await client.put(f"/warmup-preferences/user/{warmup_exercises[1].id}", headers=auth_headers,
                                   json=preference({"count": 2}, row_id=payload["id"]))
    assert foreign_key.status_code == 404
    listed = (await client.get("/warmup-preferences", headers=auth_headers)).json()
    assert len(listed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{"count": 0}, {"count": 2.5}, {"between_seconds": -1},
                                         {"before_work": {"mode": "seconds", "seconds": -1}}])
async def test_invalid_override_rejected(client, auth_headers, warmup_exercises, override):
    response = await client.put(f"/warmup-preferences/global/{warmup_exercises[0].id}", headers=auth_headers,
                                json=preference(override))
    assert response.status_code == 422
    assert (await client.get("/warmup-preferences", headers=auth_headers)).json() == []


@pytest.mark.asyncio
async def test_preference_owner_isolation(client, auth_headers, warmup_exercises, db, test_user):
    from api.schemas.warmups import WarmupPreferencePayload
    from api.services.warmup_preferences import put_exercise_warmup_preference
    global_ex, _, foreign_ex = warmup_exercises
    foreign_payload = preference({"count": 5})
    await put_exercise_warmup_preference(db, foreign_ex.app_user_id, "global", global_ex.id,
                                         WarmupPreferencePayload.model_validate(foreign_payload))
    assert (await client.get("/warmup-preferences", headers=auth_headers)).json() == []
    forbidden = await client.put(f"/warmup-preferences/global/{global_ex.id}", headers=auth_headers,
                                 json=foreign_payload)
    assert forbidden.status_code == 404
    own = await client.put(f"/warmup-preferences/global/{global_ex.id}", headers=auth_headers,
                           json=preference({"count": 2}))
    assert own.status_code == 200
    listed = (await client.get("/warmup-preferences", headers=auth_headers)).json()
    assert listed == [own.json()]
    assert listed[0]["id"] != foreign_payload["id"]


@pytest.mark.asyncio
async def test_concurrent_create_retries_and_competing_uuid(client, auth_headers, warmup_exercises):
    import asyncio
    path = f"/warmup-preferences/global/{warmup_exercises[0].id}"
    payload = preference({"count": 3})
    results = await asyncio.gather(*[client.put(path, headers=auth_headers, json=payload) for _ in range(2)])
    assert [response.status_code for response in results] == [200, 200]
    assert results[0].json() == results[1].json()
    other_path = f"/warmup-preferences/user/{warmup_exercises[1].id}"
    results = await asyncio.gather(*[client.put(other_path, headers=auth_headers, json=preference({"count": count}))
                                   for count in (2, 4)])
    assert sorted(response.status_code for response in results) == [200, 409]
    winner = next(response.json() for response in results if response.status_code == 200)
    loser = next(response.json() for response in results if response.status_code == 409)
    assert loser["detail"]["current"] == winner
    assert len((await client.get("/warmup-preferences", headers=auth_headers)).json()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("warmup", [{"compound_count": 0}, {"isolation_count": 2.5},
                                       {"between_seconds": -1}, {"before_work": {"mode": "seconds", "seconds": -1}}])
async def test_invalid_global_patch_preserves_profile(client, auth_headers, warmup_profile, db, warmup):
    response = await client.patch("/profile/settings", headers=auth_headers,
                                  json={"language": "ru", "warmup": warmup})
    assert response.status_code == 422
    await db.refresh(warmup_profile)
    assert warmup_profile.settings == {"language": "en"}


@pytest.mark.asyncio
@pytest.mark.parametrize("first_patch,second_patch,expected_language,expected_isolation", [
    ({"language": "ru"}, {"warmup": {"compound_count": 4}}, "ru", 2),
    ({"warmup": {"isolation_count": 5}}, {"warmup": {"compound_count": 4}}, "en", 5),
], ids=["legacy-language-and-warmup", "disjoint-warmup-fields"])
async def test_overlapping_settings_patches_preserve_both_updates(
    client, auth_headers, warmup_profile, db, first_patch, second_patch,
    expected_language, expected_isolation,
):
    """Force an old read to overlap a real second HTTP write, then check storage.

    Pause request A immediately after its real profile SELECT. Request B either
    commits before A resumes (the lost-update bug), or waits for A in PostgreSQL
    (serialization). Release A only after one of these observable states; no
    fake query result or lock-call assertion determines the test's outcome.
    """
    import asyncio

    from fastapi import Request
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.deps import get_db
    from api.main import app
    from app.database import engine

    warmup_profile.settings = {"language": "en", "warmup": DEFAULTS,
                               "unknown_setting": {"enabled": True}}
    await db.commit()
    first_read = asyncio.Event()
    release_first = asyncio.Event()
    second_query = asyncio.Event()
    second_pid = None

    class ScheduledSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal second_pid
            is_profile_select = any(
                entry.get("entity") is AppUserProfile
                for entry in getattr(statement, "column_descriptions", ())
            )
            role = self.info.get("settings_race_role")
            intercept = is_profile_select and not self.info.get("profile_read_seen")
            if intercept:
                self.info["profile_read_seen"] = True
                if role == "second":
                    second_pid = (await super().execute(text("SELECT pg_backend_pid()"))).scalar_one()
                    second_query.set()
            result = await super().execute(statement, *args, **kwargs)
            if intercept and role == "first":
                first_read.set()
                await release_first.wait()
            return result

    async def scheduled_db(request: Request):
        async with ScheduledSession(bind=engine, expire_on_commit=False,
                                    info={"settings_race_role": request.headers.get("x-settings-race-role")}) as session:
            yield session

    async def second_finished_or_waiting(second_task):
        await second_query.wait()
        while not second_task.done():
            blocked = (await db.execute(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": second_pid},
            )).scalar_one()
            if blocked:
                return
            await asyncio.sleep(0.01)

    old_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = scheduled_db
    tasks = []
    try:
        first_task = asyncio.create_task(client.patch(
            "/profile/settings", headers={**auth_headers, "x-settings-race-role": "first"}, json=first_patch,
        ))
        tasks.append(first_task)
        await asyncio.wait_for(first_read.wait(), timeout=10)
        second_task = asyncio.create_task(client.patch(
            "/profile/settings", headers={**auth_headers, "x-settings-race-role": "second"}, json=second_patch,
        ))
        tasks.append(second_task)
        await asyncio.wait_for(second_finished_or_waiting(second_task), timeout=10)
        release_first.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        assert [response.status_code for response in responses] == [200, 200]
    finally:
        release_first.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if old_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = old_override

    settings = (await client.get("/profile", headers=auth_headers)).json()["settings"]
    assert settings["warmup"] == {"compound_count": 4, "isolation_count": expected_isolation,
                                  "between_seconds": 30, "before_work": {"mode": "working"}}
    assert settings["language"] == expected_language
    assert settings["unknown_setting"] == {"enabled": True}
    await db.refresh(warmup_profile)
    assert warmup_profile.settings == settings
