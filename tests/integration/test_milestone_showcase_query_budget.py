"""Бюджет запросов витрины вех (P1-03 ч.2, снижение стоимости экрана).

Разовый замер (_measure_showcase.py, удалён из репозитория после того, как
это число закрепил тест) показал 75 запросов на пользователя с историей по
всем шести движениям — по 15 на карточку, из них большая часть чистое
дублирование внутри evaluate()/scheme_context() и между карточками
build_showcase (см. отчёт .superpowers/sdd/perf-showcase-report.md).
Дедупликация (api/services/goal/service.py, api/services/goal/repository.py,
api/services/milestones/service.py) свела это к 51. Без теста это число
ничем не защищено — правка, вернувшая двойное чтение current_e1rm/
exercise_context/профиля/активного блока, прошла бы незамеченной до
следующего ручного замера.

Считаем запросы тем же приёмом, что и разовый замер: SQLAlchemy-событие
`before_cursor_execute` на sync-движке под async-сессией.
"""
import pytest
from datetime import date, datetime, timezone

from sqlalchemy import event

from api.services.milestones.catalog import LIFTS
from api.services.milestones.repository import resolve_lift_exercises
from api.services.milestones.service import build_showcase
from api.services.models import (
    AppUserProfile, UserAnthropometry, UserExerciseProgressionState,
)
from app.database import SessionLocal, engine

pytestmark = pytest.mark.asyncio

# Достигнуто после дедупликации: 51 запрос. Граница — достигнутое + 10%
# (округлено вверх), чтобы тест не сыпался от несущественного шума (лишний
# запрос на одну карточку из-за легитимной новой фичи), но ловил возврат к
# прежнему порядку дублирования.
#
# Если тест упал: не поднимай границу не глядя. Сначала проверь, не
# вернулась ли одна из трёх дедуплицированных нагрузок —
#   1) evaluate()/scheme_context() в api/services/goal/service.py и
#      api/services/goal/repository.py читают current_e1rm и
#      exercise_context по ОДНОМУ разу за вызов (параметры
#      known_working_e1rm/exercise_ctx у scheme_context);
#   2) build_showcase (api/services/milestones/service.py) читает профиль
#      пользователя ОДИН раз и передаёт его в evaluate() каждой карточки
#      (параметр profile);
#   3) build_showcase читает активный блок (microcycle_length) ОДИН раз и
#      передаёт его так же (параметр microcycle_length).
# Только если рост запросов объясняется новой легитимной фичей — подними
# MAX_QUERIES и запиши новое достигнутое число в отчёте.
MAX_QUERIES = 56


async def test_build_showcase_query_budget(test_user):
    """build_showcase не должен снова расползтись до ~15 запросов на карточку."""
    async with SessionLocal() as db:
        db.add(AppUserProfile(
            app_user_id=test_user.id, experience_level="intermediate",
            settings={}, volume_budget={"meta": {}},
        ))
        db.add(UserAnthropometry(
            app_user_id=test_user.id, weight=80.0,
            recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        ))
        resolved = await resolve_lift_exercises(db)
        assert set(resolved) == set(LIFTS), "в каталоге должны быть все шесть движений"
        for _lift, exercise_id in resolved.items():
            db.add(UserExerciseProgressionState(
                app_user_id=test_user.id, exercise_id=exercise_id, working_e1rm=90.0,
            ))
        await db.commit()

    count = 0

    def _count(conn, cursor, statement, parameters, context, executemany):
        nonlocal count
        count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        async with SessionLocal() as db:
            cards = (await build_showcase(db, test_user.id, date.today())).cards
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)

    assert cards, "витрина не должна быть пустой при истории по всем движениям"
    assert count <= MAX_QUERIES, (
        f"build_showcase сделал {count} запросов на {len(cards)} карточек, "
        f"граница {MAX_QUERIES} — см. докстринг MAX_QUERIES про то, что проверить"
    )
