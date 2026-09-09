"""Условия принятой вехи доезжают до генератора (P1-03 ч.2, §5.3, §10.4)."""
import pytest
from datetime import datetime, timezone

from sqlalchemy import select

from api.services.milestones.conditions import conditions_for
from api.services.milestones.service import milestone_accents
from api.services.models import AppUserProfile, UserAnthropometry
from app.database import SessionLocal



async def _prepare(user_id: int) -> None:
    async with SessionLocal() as db:
        db.add(UserAnthropometry(
            app_user_id=user_id, weight=80.0,
            recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ))
        db.add(AppUserProfile(app_user_id=user_id, experience_level="intermediate",
                              settings={}))
        await db.commit()


@pytest.mark.asyncio
async def test_accept_stores_the_conditions(client, auth_headers, test_user):
    await _prepare(test_user.id)
    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 201, r.text

    async with SessionLocal() as db:
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalars().first()
    stored = (profile.settings or {}).get("milestone") or {}
    expected = conditions_for("squat")
    assert stored["accents"] == list(expected.accents)
    assert stored["split_requirement"] == expected.split_requirement


def test_accents_reader_tolerates_empty_settings():
    assert milestone_accents(None) == []
    assert milestone_accents({}) == []
    assert milestone_accents({"milestone": {}}) == []
    assert milestone_accents({"milestone": {"accents": ["quads"]}}) == ["quads"]


@pytest.mark.asyncio
async def test_generation_uses_milestone_accents_when_request_is_silent(
    client, auth_headers, test_user,
):
    """Запрос без accent_muscles должен унаследовать акценты вехи."""
    await _prepare(test_user.id)
    assert (await client.post("/goals/milestones/squat_2x_bw/accept",
                              headers=auth_headers)).status_code == 201

    async with SessionLocal() as db:
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalars().first()
        accents = milestone_accents(profile.settings)

    assert accents == list(conditions_for("squat").accents)


class _Config:
    """Минимальная замена конфигу генерации: хелперу нужны только два поля."""

    def __init__(self, accent_muscles=None, accent_muscle=None):
        self.accent_muscles = accent_muscles
        self.accent_muscle = accent_muscle


class _Profile:
    """Минимальная замена профилю: хелперу нужны бюджет и настройки."""

    def __init__(self, milestone=None, focus_muscles=None):
        self.settings = {"milestone": milestone} if milestone else {}
        self.volume_budget = {"meta": {"focus_muscles": focus_muscles or []}}


def test_explicit_choice_beats_the_milestone():
    """Веха задаёт умолчание, а не диктат: выбор человека сильнее.

    Это ядро задачи, и проверять его надо на самом хелпере: через эндпоинт
    видно только то, что акценты СОХРАНИЛИСЬ, а не то, какие из них победят
    при генерации.
    """
    from api.routers.plans import _resolve_accents

    profile = _Profile(milestone={"accents": ["quads", "glutes"]})
    assert _resolve_accents(_Config(accent_muscles=["chest"]), profile) == ["chest"]
    assert _resolve_accents(_Config(accent_muscle="triceps"), profile) == ["triceps"]


def test_milestone_beats_profile_focus_muscles():
    """Цель, поставленная человеком сейчас, конкретнее давней общей настройки."""
    from api.routers.plans import _resolve_accents

    profile = _Profile(
        milestone={"accents": ["quads", "glutes"]},
        focus_muscles=["chest", "biceps"],
    )
    assert _resolve_accents(_Config(), profile) == ["quads", "glutes"]


def test_without_a_milestone_nothing_changes_for_the_profile():
    """Прежнее поведение обязано сохраниться дословно для тех, кто вех не брал."""
    from api.routers.plans import _resolve_accents

    profile = _Profile(focus_muscles=["chest", "biceps"])
    assert _resolve_accents(_Config(), profile) == ["chest", "biceps"]
    assert _resolve_accents(_Config(), _Profile()) == []


def test_accents_are_capped_at_two():
    """Генератор больше двух не различает — значит порядок в таблице значим."""
    from api.routers.plans import _resolve_accents

    cond = conditions_for("deadlift")
    assert len(cond.accents) > 2, "у становой в таблице больше двух акцентов"
    profile = _Profile(milestone={"accents": list(cond.accents)})
    assert _resolve_accents(_Config(), profile) == list(cond.accents[:2])
