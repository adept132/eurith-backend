"""Загрузка данных витрины из БД (P1-03 ч.2, §5.1, §5.2)."""
import pytest
from datetime import datetime, timezone

from api.services.milestones.catalog import LIFTS
from api.services.milestones.repository import (
    current_e1rm_by_lift, experience_and_cap, latest_bodyweight,
    resolve_lift_exercises,
)
from api.services.models import (
    AppUserProfile, UserAnthropometry, UserExerciseProgressionState,
)
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def test_all_six_lifts_resolve_to_system_exercises():
    """Если упражнение переименуют, витрина потеряет движение молча —
    этот тест ловит такое переименование сразу."""
    async with SessionLocal() as db:
        resolved = await resolve_lift_exercises(db)
    assert set(resolved) == set(LIFTS), f"не разрешились: {set(LIFTS) - set(resolved)}"
    assert all(isinstance(v, int) for v in resolved.values())


async def test_e1rm_is_read_per_lift_and_missing_history_is_absent(test_user):
    async with SessionLocal() as db:
        resolved = await resolve_lift_exercises(db)
        db.add(UserExerciseProgressionState(
            app_user_id=test_user.id,
            exercise_id=resolved["squat"],
            working_e1rm=120.0,
        ))
        await db.commit()

    async with SessionLocal() as db:
        resolved = await resolve_lift_exercises(db)
        by_lift = await current_e1rm_by_lift(db, test_user.id, resolved)

    assert by_lift["squat"] == 120.0
    assert "bench" not in by_lift, "движение без истории в словаре не появляется"


async def test_bodyweight_takes_the_latest_record(test_user):
    async with SessionLocal() as db:
        db.add(UserAnthropometry(
            app_user_id=test_user.id, weight=80.0,
            recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ))
        db.add(UserAnthropometry(
            app_user_id=test_user.id, weight=85.0,
            recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ))
        await db.commit()

    async with SessionLocal() as db:
        assert await latest_bodyweight(db, test_user.id) == 85.0


async def test_bodyweight_breaks_ties_by_insertion_order(test_user):
    """Офлайн-синк шлёт пачку с одной меткой времени — берём вставленную последней."""
    async with SessionLocal() as db:
        stamp = datetime(2026, 6, 1, tzinfo=timezone.utc)
        db.add(UserAnthropometry(app_user_id=test_user.id, weight=70.0, recorded_at=stamp))
        await db.flush()
        db.add(UserAnthropometry(app_user_id=test_user.id, weight=90.0, recorded_at=stamp))
        await db.commit()

    async with SessionLocal() as db:
        assert await latest_bodyweight(db, test_user.id) == 90.0


async def test_bodyweight_is_none_without_records(test_user):
    async with SessionLocal() as db:
        assert await latest_bodyweight(db, test_user.id) is None


async def test_cap_comes_from_experience_level(test_user):
    async with SessionLocal() as db:
        db.add(AppUserProfile(app_user_id=test_user.id, experience_level="advanced",
                              settings={}))
        await db.commit()

    async with SessionLocal() as db:
        level, cap = await experience_and_cap(db, test_user.id)
    assert level == "advanced"
    assert cap == 0.003, "опытные растут медленнее, потолок из WEEKLY_GROWTH_CAP_PCT"


async def test_missing_profile_degrades_to_beginner(test_user):
    async with SessionLocal() as db:
        level, cap = await experience_and_cap(db, test_user.id)
    assert level == "beginner"
    assert cap == 0.01
