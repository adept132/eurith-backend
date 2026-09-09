"""Витрина вех: сборка и эндпоинт (P1-03 ч.2, §5.2, §6.1)."""
import pytest
from datetime import date, datetime, timezone

from api.services.milestones.repository import resolve_lift_exercises
from api.services.milestones.service import build_showcase
from api.services.models import (
    AppUserProfile, UserAnthropometry, UserExerciseProgressionState,
)
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _set_bodyweight(user_id: int, kg: float) -> None:
    async with SessionLocal() as db:
        db.add(UserAnthropometry(
            app_user_id=user_id, weight=kg,
            recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ))
        await db.commit()


async def _set_e1rm(user_id: int, lift: str, value: float) -> None:
    async with SessionLocal() as db:
        resolved = await resolve_lift_exercises(db)
        db.add(UserExerciseProgressionState(
            app_user_id=user_id, exercise_id=resolved[lift], working_e1rm=value,
        ))
        await db.commit()


async def _set_level(user_id: int, level: str) -> None:
    async with SessionLocal() as db:
        db.add(AppUserProfile(app_user_id=user_id, experience_level=level, settings={}))
        await db.commit()


async def test_showcase_carries_conditions_for_every_card(test_user):
    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")

    async with SessionLocal() as db:
        cards = await build_showcase(db, test_user.id, date.today())

    assert cards, "витрина не должна быть пустой"
    for c in cards:
        assert c.accents, f"{c.code}: акценты обязаны прийти на карточку"
        assert set(c.split_requirement) == {"any_of", "min"}
        assert c.mesocycle_preset


async def test_card_without_history_has_no_numbers(test_user):
    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")

    async with SessionLocal() as db:
        cards = await build_showcase(db, test_user.id, date.today())

    assert all(c.remaining is None for c in cards)
    assert all(c.suggested_deadline is None for c in cards)
    assert all(c.has_history is False for c in cards)


async def test_card_with_history_reports_the_gap(test_user):
    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")
    await _set_e1rm(test_user.id, "bench", 87.5)

    async with SessionLocal() as db:
        cards = await build_showcase(db, test_user.id, date.today())

    bench = [c for c in cards if c.lift == "bench"][0]
    assert bench.has_history is True
    assert bench.remaining == 12.5
    assert bench.target == 100.0


async def test_endpoint_returns_the_same_cards(client, auth_headers, test_user):
    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")

    r = await client.get("/goals/milestones", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body, "витрина не должна быть пустой"
    assert {"code", "lift", "title", "target", "remaining", "target_reps",
            "has_history", "suggested_deadline", "accents",
            "split_requirement", "mesocycle_preset"} <= set(body[0])


async def test_milestones_route_is_not_swallowed_by_the_goal_id_route(
    client, auth_headers, test_user,
):
    """GET /goals/{goal_id} не должен разобрать слово milestones как id."""
    r = await client.get("/goals/milestones", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
