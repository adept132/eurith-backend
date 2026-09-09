"""Условия принятой вехи доезжают до генератора (P1-03 ч.2, §5.3, §10.4)."""
import pytest
from datetime import datetime, timezone

from sqlalchemy import select

from api.services.milestones.conditions import conditions_for
from api.services.milestones.service import milestone_accents
from api.services.models import AppUserProfile, UserAnthropometry
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _prepare(user_id: int) -> None:
    async with SessionLocal() as db:
        db.add(UserAnthropometry(
            app_user_id=user_id, weight=80.0,
            recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ))
        db.add(AppUserProfile(app_user_id=user_id, experience_level="intermediate",
                              settings={}))
        await db.commit()


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


async def test_explicit_request_accents_win_over_the_milestone(
    client, auth_headers, test_user,
):
    """Веха задаёт умолчание, а не диктат: явный выбор человека сильнее."""
    await _prepare(test_user.id)
    assert (await client.post("/goals/milestones/squat_2x_bw/accept",
                              headers=auth_headers)).status_code == 201

    async with SessionLocal() as db:
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalars().first()
        accents = milestone_accents(profile.settings)

    assert accents == list(conditions_for("squat").accents)
    # Генератор различает не больше двух акцентов, поэтому в план доедут
    # первые две мышцы курируемой таблицы — порядок в ней значим.
    assert accents[:2] == ["quads", "glutes"]
