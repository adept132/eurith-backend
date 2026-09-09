"""Дрейф веса тела относительно момента принятия вехи (P1-03 ч.2, §5.5)."""
import pytest
from datetime import datetime, timezone

from api.services.models import AppUserProfile, UserAnthropometry
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _prepare(user_id: int, kg: float) -> None:
    async with SessionLocal() as db:
        db.add(UserAnthropometry(
            app_user_id=user_id, weight=kg,
            recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ))
        db.add(AppUserProfile(app_user_id=user_id, experience_level="intermediate",
                              settings={}))
        await db.commit()


async def _add_weight(user_id: int, kg: float, when: datetime) -> None:
    async with SessionLocal() as db:
        db.add(UserAnthropometry(app_user_id=user_id, weight=kg, recorded_at=when))
        await db.commit()


async def test_no_drift_without_an_accepted_milestone(client, auth_headers, test_user):
    await _prepare(test_user.id, 80.0)
    r = await client.get("/goals/milestones/drift", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() is None


async def test_small_drift_is_silent(client, auth_headers, test_user):
    """Меньше 5 % — колебание воды, а не повод трогать цель."""
    await _prepare(test_user.id, 80.0)
    assert (await client.post("/goals/milestones/squat_2x_bw/accept",
                              headers=auth_headers)).status_code == 201
    await _add_weight(test_user.id, 82.0, datetime(2026, 7, 1, tzinfo=timezone.utc))

    r = await client.get("/goals/milestones/drift", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() is None


async def test_large_drift_offers_the_new_target(client, auth_headers, test_user):
    await _prepare(test_user.id, 80.0)
    assert (await client.post("/goals/milestones/squat_2x_bw/accept",
                              headers=auth_headers)).status_code == 201
    await _add_weight(test_user.id, 86.0, datetime(2026, 7, 1, tzinfo=timezone.utc))

    r = await client.get("/goals/milestones/drift", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body is not None
    assert body["stored_bodyweight"] == 80.0
    assert body["current_bodyweight"] == 86.0
    assert body["current_target"] == 172.0, "двойной вес при 86 кг"
    assert body["goal_id"]


async def test_absolute_milestone_never_drifts(client, auth_headers, test_user):
    """Сотка от веса тела не зависит — строка не появляется никогда."""
    await _prepare(test_user.id, 80.0)
    assert (await client.post("/goals/milestones/bench_100kg/accept",
                              headers=auth_headers)).status_code == 201
    await _add_weight(test_user.id, 95.0, datetime(2026, 7, 1, tzinfo=timezone.utc))

    r = await client.get("/goals/milestones/drift", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() is None


async def test_drift_disappears_when_the_goal_is_gone(client, auth_headers, test_user):
    """Ведущей цели нет — правку предлагать нечему."""
    await _prepare(test_user.id, 80.0)
    assert (await client.post("/goals/milestones/squat_2x_bw/accept",
                              headers=auth_headers)).status_code == 201
    await _add_weight(test_user.id, 86.0, datetime(2026, 7, 1, tzinfo=timezone.utc))

    from sqlalchemy import delete
    from api.services.models import UserGoal
    async with SessionLocal() as db:
        await db.execute(delete(UserGoal).where(UserGoal.app_user_id == test_user.id))
        await db.commit()

    r = await client.get("/goals/milestones/drift", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json() is None
