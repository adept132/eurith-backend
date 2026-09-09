"""Принятие вехи: ведущая цель, дешёвые рычаги, память о весе (§5.4)."""
import pytest
from datetime import datetime, timezone

from sqlalchemy import select

from api.services.milestones.repository import resolve_lift_exercises
from api.services.models import (
    AppUserProfile, UserAnthropometry, UserExercisePreference, UserGoal,
)
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _prepare(user_id: int, kg: float | None = 80.0) -> None:
    async with SessionLocal() as db:
        if kg is not None:
            db.add(UserAnthropometry(
                app_user_id=user_id, weight=kg,
                recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            ))
        db.add(AppUserProfile(app_user_id=user_id, experience_level="intermediate",
                              settings={}))
        await db.commit()


async def test_accept_creates_a_primary_strength_goal(client, auth_headers, test_user):
    await _prepare(test_user.id)

    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 201, r.text

    async with SessionLocal() as db:
        goals = (await db.execute(
            select(UserGoal).where(UserGoal.app_user_id == test_user.id)
        )).scalars().all()
    assert len(goals) == 1
    goal = goals[0]
    assert goal.goal_type == "strength"
    assert goal.is_primary is True
    assert goal.target_value == 160.0, "двойной вес при 80 кг"
    assert goal.target_reps == 1


async def test_accept_marks_the_lift_as_favorite(client, auth_headers, test_user):
    """Дешёвый рычаг: тот же носитель, что у LEVER_ENSURE_PRESENT в P0-12."""
    await _prepare(test_user.id)
    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 201, r.text

    async with SessionLocal() as db:
        resolved = await resolve_lift_exercises(db)
        prefs = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id,
                UserExercisePreference.exercise_id == resolved["squat"],
            )
        )).scalars().all()
    assert [p.preference for p in prefs] == ["favorite"]


async def test_accept_remembers_the_rule_and_the_bodyweight(client, auth_headers, test_user):
    """§5.4 шаг 4: без веса на момент принятия строку дрейфа не показать."""
    await _prepare(test_user.id, kg=80.0)
    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 201, r.text

    async with SessionLocal() as db:
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalars().first()
    stored = (profile.settings or {}).get("milestone") or {}
    assert stored.get("code") == "squat_2x_bw"
    assert stored.get("bodyweight") == 80.0


async def test_accepting_a_second_milestone_replaces_the_primary(client, auth_headers, test_user):
    """Структурные рычаги не делятся между двумя хозяевами (решение 2 P0-12)."""
    await _prepare(test_user.id)
    assert (await client.post("/goals/milestones/squat_2x_bw/accept",
                              headers=auth_headers)).status_code == 201
    assert (await client.post("/goals/milestones/bench_100kg/accept",
                              headers=auth_headers)).status_code == 201

    async with SessionLocal() as db:
        primary = (await db.execute(
            select(UserGoal).where(
                UserGoal.app_user_id == test_user.id, UserGoal.is_primary.is_(True),
            )
        )).scalars().all()
    assert len(primary) == 1
    assert primary[0].target_value == 100.0, "ведущей стала сотка в жиме"


async def test_relative_milestone_without_bodyweight_is_refused(client, auth_headers, test_user):
    """Порог посчитать нечем — 409, а не цель с выдуманным числом."""
    await _prepare(test_user.id, kg=None)

    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 409, r.text

    async with SessionLocal() as db:
        goals = (await db.execute(
            select(UserGoal).where(UserGoal.app_user_id == test_user.id)
        )).scalars().all()
    assert goals == [], "отказ не должен оставлять недоделанную цель"


async def test_absolute_milestone_works_without_bodyweight(client, auth_headers, test_user):
    """Сотка от веса тела не зависит и принимается без него."""
    await _prepare(test_user.id, kg=None)
    r = await client.post("/goals/milestones/bench_100kg/accept", headers=auth_headers)
    assert r.status_code == 201, r.text


async def test_unknown_code_gives_404(client, auth_headers, test_user):
    await _prepare(test_user.id)
    r = await client.post("/goals/milestones/no-such-milestone/accept", headers=auth_headers)
    assert r.status_code == 404, r.text
