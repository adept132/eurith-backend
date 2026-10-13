"""Real profile endpoint coverage for rest timer policy storage."""
import asyncio

import pytest
import pytest_asyncio

from api.services.models import AppUserProfile


@pytest_asyncio.fixture
async def rest_profile(db, test_user):
    profile = AppUserProfile(app_user_id=test_user.id, settings={
        "language": "en", "warmup": {"compound_count": 4, "isolation_count": 2,
                                     "between_seconds": 30, "before_work": {"mode": "working"}},
        "unknown_setting": {"enabled": True},
    })
    db.add(profile)
    await db.commit()
    return profile


@pytest.mark.asyncio
async def test_profile_patch_preserves_siblings_and_old_clients(client, auth_headers, rest_profile, db):
    before = (await client.get("/profile", headers=auth_headers)).json()["settings"]
    assert before["rest_timer_policy"]["base_seconds"] == {"tier1": 180, "tier2": 120, "tier3": 90}
    await db.refresh(rest_profile)
    assert "rest_timer_policy" not in rest_profile.settings

    first = await client.patch("/profile/settings", headers=auth_headers,
                               json={"rest_timer_policy": {"base_seconds": {"tier2": 150}}})
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["settings"]["rest_timer_policy"]["base_seconds"] == {"tier1": 180, "tier2": 150, "tier3": 90}
    assert data["settings"]["warmup"] == before["warmup"]

    old = await client.patch("/profile/settings", headers=auth_headers, json={"language": "ru"})
    assert old.status_code == 200, old.text
    assert old.json()["settings"]["rest_timer_policy"] == data["settings"]["rest_timer_policy"]
    assert old.json()["settings"]["unknown_setting"] == {"enabled": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    None, {"version": 2}, {"base_seconds": {"tier1": None}},
    {"base_seconds": {"tier1": True}}, {"base_seconds": {"tier1": 180.5}},
    {"base_seconds": {"tier1": 187}}, {"effort_seconds": {"failure": 181}},
])
async def test_bad_policy_patch_rejected_without_write(client, auth_headers, rest_profile, db, bad):
    response = await client.patch("/profile/settings", headers=auth_headers,
                                  json={"language": "ru", "rest_timer_policy": bad})
    assert response.status_code == 422, response.text
    await db.refresh(rest_profile)
    assert rest_profile.settings["language"] == "en"


@pytest.mark.asyncio
async def test_parallel_nested_patches_preserve_both(client, auth_headers, rest_profile, db):
    from fastapi import Request
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.deps import get_db
    from api.main import app
    from app.database import engine

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
            role = self.info.get("rest_race_role")
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
                                    info={"rest_race_role": request.headers.get("x-rest-race-role")}) as session:
            yield session

    async def second_finished_or_waiting(task):
        await second_query.wait()
        while not task.done():
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
        first = asyncio.create_task(client.patch(
            "/profile/settings", headers={**auth_headers, "x-rest-race-role": "first"},
            json={"rest_timer_policy": {"base_seconds": {"tier2": 150}}},
        ))
        tasks.append(first)
        await asyncio.wait_for(first_read.wait(), timeout=10)
        second = asyncio.create_task(client.patch(
            "/profile/settings", headers={**auth_headers, "x-rest-race-role": "second"},
            json={"rest_timer_policy": {"effort_seconds": {"failure": 90}}},
        ))
        tasks.append(second)
        await asyncio.wait_for(second_finished_or_waiting(second), timeout=10)
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
    assert settings["rest_timer_policy"]["base_seconds"]["tier2"] == 150
    assert settings["rest_timer_policy"]["effort_seconds"]["failure"] == 90
    assert settings["warmup"]["compound_count"] == 4
    assert settings["unknown_setting"] == {"enabled": True}
    await db.refresh(rest_profile)
    assert rest_profile.settings == settings
