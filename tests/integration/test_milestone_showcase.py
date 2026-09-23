"""Витрина вех: сборка и эндпоинт (P1-03 ч.2, §5.2, §6.1)."""
import pytest
from datetime import date, datetime, timezone

from sqlalchemy import select

from api.services.milestones.repository import resolve_lift_exercises
from api.services.milestones.service import build_showcase
from api.services.models import (
    AppUserMesocycle, AppUserProfile, Mesocycle, UserAnthropometry,
    UserExerciseProgressionState, UserGoal,
)
from app.database import SessionLocal

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("milestone_catalog")]


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
        cards = (await build_showcase(db, test_user.id, date.today())).cards

    assert cards, "витрина не должна быть пустой"
    for c in cards:
        assert c.accents, f"{c.code}: акценты обязаны прийти на карточку"
        assert set(c.split_requirement) == {"any_of", "min"}
        assert c.mesocycle_preset


async def test_card_without_history_has_no_numbers(test_user):
    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")

    async with SessionLocal() as db:
        cards = (await build_showcase(db, test_user.id, date.today())).cards

    assert all(c.remaining is None for c in cards)
    assert all(c.suggested_deadline is None for c in cards)
    assert all(c.has_history is False for c in cards)


async def test_card_with_history_reports_the_gap(test_user):
    """Остаток — разница e1RM, не «целевые кг минус current» (финальное
    ревью, Critical): target_e1rm(bench_100kg) = 100 × 31/30 = 103.3(3) ->
    103.3; remaining = 103.3 - 87.5 = 15.8. `target` на карточке остаётся
    голыми килограммами штанги (100.0) — меняется только то, по чему
    считается остаток."""
    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")
    await _set_e1rm(test_user.id, "bench", 87.5)

    async with SessionLocal() as db:
        cards = (await build_showcase(db, test_user.id, date.today())).cards

    bench = [c for c in cards if c.lift == "bench"][0]
    assert bench.has_history is True
    assert bench.remaining == 15.8
    assert bench.target == 100.0


async def test_showcase_keeps_pullups_without_bodyweight(test_user):
    """Финальное ревью, Important 5: без веса тела остаются абсолютные вехи
    И подтягивания — подтягивание считается числом повторов, а не весом."""
    await _set_level(test_user.id, "intermediate")
    # Веса тела намеренно не заводим — сравни с _set_bodyweight в других тестах.

    async with SessionLocal() as db:
        cards = (await build_showcase(db, test_user.id, date.today())).cards

    lifts = {c.lift for c in cards}
    assert "pullup" in lifts, "подтягивание не должно прятаться без веса тела"
    pullup = [c for c in cards if c.lift == "pullup"][0]
    assert pullup.code == "pullup_first"
    assert pullup.target is None
    assert pullup.remaining is None


async def test_endpoint_returns_the_same_cards(client, auth_headers, test_user):
    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")

    r = await client.get("/goals/milestones", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cards"], "витрина не должна быть пустой"
    assert {"code", "lift", "title", "target", "remaining", "target_reps",
            "has_history", "suggested_deadline", "accents",
            "split_requirement", "mesocycle_preset"} <= set(body["cards"][0])


async def test_milestones_route_is_not_swallowed_by_the_goal_id_route(
    client, auth_headers, test_user,
):
    """GET /goals/{goal_id} не должен разобрать слово milestones как id."""
    r = await client.get("/goals/milestones", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["cards"], list)


async def _activate_mesocycle(user_id: int, preset_name: str) -> None:
    """Даёт пользователю активный мезоцикл — личную копию системного пресета.

    Опознание в сервисе идёт по имени (см. _active_mesocycle_code), поэтому
    для теста достаточно завести Mesocycle с именем пресета и одну фазу —
    состав фаз на опознание не влияет.
    """
    async with SessionLocal() as db:
        meso = Mesocycle(
            author_id=user_id, name=preset_name,
            code=f"test-{preset_name}", phases_in_cycle=1,
        )
        db.add(meso)
        await db.flush()
        db.add(AppUserMesocycle(
            app_user_id=user_id, mesocycle_id=meso.id, is_active=True,
            microcycle_length=7, current_phase=1,
        ))
        await db.commit()


async def test_endpoint_carries_the_active_mesocycle_preset_code(
    client, auth_headers, test_user,
):
    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")
    await _activate_mesocycle(test_user.id, "Силовой")

    r = await client.get("/goals/milestones", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["active_mesocycle_preset"] == "strength"


async def test_endpoint_reports_null_preset_when_no_active_mesocycle(
    client, auth_headers, test_user,
):
    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")

    r = await client.get("/goals/milestones", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["active_mesocycle_preset"] is None


async def test_card_with_history_carries_the_simulated_deadline(
    test_user, monkeypatch,
):
    """Срок на карточке — это дата пересечения из симуляции, а не выдумка."""
    import api.services.milestones.service as milestones_service

    async def _fake_evaluate(session, app_user_id, goal, today, **kwargs):
        return {"eta": date(2026, 12, 1)}

    monkeypatch.setattr(milestones_service, "evaluate", _fake_evaluate)

    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")
    await _set_e1rm(test_user.id, "bench", 87.5)

    async with SessionLocal() as db:
        cards = (await build_showcase(db, test_user.id, date.today())).cards

    bench = [c for c in cards if c.lift == "bench"][0]
    assert bench.suggested_deadline == date(2026, 12, 1)


async def test_accepted_goal_gets_the_simulated_deadline(
    client, auth_headers, test_user, monkeypatch,
):
    """Срок из симуляции обязан попасть в цель — иначе автопилот к ней слеп."""
    import api.services.milestones.service as milestones_service

    async def _fake_evaluate(session, app_user_id, goal, today):
        return {"eta": date(2026, 12, 1)}

    monkeypatch.setattr(milestones_service, "evaluate", _fake_evaluate)

    await _set_bodyweight(test_user.id, 80.0)
    await _set_level(test_user.id, "intermediate")

    r = await client.post("/goals/milestones/bench_100kg/accept", headers=auth_headers)
    assert r.status_code == 201, r.text

    async with SessionLocal() as db:
        goal = (await db.execute(
            select(UserGoal).where(UserGoal.app_user_id == test_user.id)
        )).scalars().first()
    assert goal.deadline == date(2026, 12, 1)
