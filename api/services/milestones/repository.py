"""Единственный файл пакета milestones/, ходящий в БД.

Та же конвенция, что у structure/ и goal/: чистые модули считают, репозиторий
приносит данные. Ни одна функция здесь не коммитит — это забота вызывающей
стороны.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.forecast_service import WEEKLY_GROWTH_CAP_PCT, _DEFAULT_CAP_PCT
from api.services.milestones.catalog import LIFTS
from api.services.models import (
    AppUserProfile, Exercise, UserAnthropometry, UserExerciseProgressionState,
    WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet,
)


async def resolve_lift_exercises(session: AsyncSession) -> dict[str, int]:
    """Код движения -> id системного упражнения.

    Движения, чьего упражнения в каталоге нет, в словарь не попадают: веха по
    ним просто не показывается, а не роняет витрину. Ищем среди системных
    (app_user_id IS NULL) — одноимённое пользовательское упражнение не должно
    перехватывать веху.
    """
    rows = (await session.execute(
        select(Exercise.id, Exercise.name).where(
            Exercise.name.in_(list(LIFTS.values())),
            Exercise.app_user_id.is_(None),
        )
    )).all()
    by_name = {name: eid for eid, name in rows}
    return {
        lift: by_name[name]
        for lift, name in LIFTS.items()
        if name in by_name
    }


async def current_e1rm_by_lift(
    session: AsyncSession, app_user_id: int, lift_exercise_ids: dict[str, int]
) -> dict[str, float]:
    """Рабочий e1RM по коду движения, одним запросом на все шесть.

    Отсутствие ключа значит «истории по этому движению нет» — то же различие,
    что делает goal/repository.current_e1rm, только пакетно.
    """
    if not lift_exercise_ids:
        return {}
    by_exercise = dict((await session.execute(
        select(
            UserExerciseProgressionState.exercise_id,
            UserExerciseProgressionState.working_e1rm,
        ).where(
            UserExerciseProgressionState.app_user_id == app_user_id,
            UserExerciseProgressionState.exercise_id.in_(list(lift_exercise_ids.values())),
        )
    )).all())
    return {
        lift: float(by_exercise[eid])
        for lift, eid in lift_exercise_ids.items()
        if by_exercise.get(eid) is not None
    }


async def completed_sets_by_lift(
    session: AsyncSession, app_user_id: int, lift_exercise_ids: dict[str, int],
) -> dict[str, list[tuple[Optional[float], int]]]:
    """Best logged weight at each rep count, across all finished workouts.

    Uses the same eligibility rules as personal records. Grouping in the DB
    keeps old workout histories from inflating the showcase response cost.
    """
    if not lift_exercise_ids:
        return {}
    rows = (await session.execute(
        select(
            WorkoutSessionExercise.exercise_id,
            WorkoutSessionSet.reps,
            func.max(WorkoutSessionSet.weight),
        )
        .select_from(WorkoutSessionSet)
        .join(WorkoutSessionExercise)
        .join(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "finished",
            WorkoutSession.finished_at.is_not(None),
            WorkoutSessionExercise.exercise_id.in_(list(lift_exercise_ids.values())),
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.is_anomalous.is_(False),
            WorkoutSessionSet.set_type == "normal",
            WorkoutSessionSet.reps.is_not(None),
            WorkoutSessionSet.reps > 0,
        )
        .group_by(WorkoutSessionExercise.exercise_id, WorkoutSessionSet.reps)
    )).all()
    by_id: dict[int, list[tuple[Optional[float], int]]] = {}
    for exercise_id, reps, weight in rows:
        by_id.setdefault(exercise_id, []).append(
            (float(weight) if weight is not None else None, int(reps))
        )
    return {
        lift: by_id[exercise_id]
        for lift, exercise_id in lift_exercise_ids.items()
        if exercise_id in by_id
    }


async def latest_bodyweight(session: AsyncSession, app_user_id: int) -> Optional[float]:
    """Последний записанный вес тела. Таблица append-only, берём свежую запись."""
    row = (await session.execute(
        select(UserAnthropometry.weight)
        .where(
            UserAnthropometry.app_user_id == app_user_id,
            UserAnthropometry.weight.is_not(None),
        )
        # Тай-брейк по id: таблица append-only, и офлайн-синк присылает пачку
        # записей с ОДНОЙ клиентской меткой времени. Postgres при равном
        # ключе сортировки порядок не гарантирует, так что без второго поля
        # «последний вес» выбирался бы произвольно.
        .order_by(UserAnthropometry.recorded_at.desc(), UserAnthropometry.id.desc())
        .limit(1)
    )).scalars().first()
    return float(row) if row is not None else None


async def experience_and_cap(
    session: AsyncSession, app_user_id: int, *, profile: Optional[AppUserProfile] = None,
) -> tuple[str, float]:
    """Уровень и биологический потолок недельного прироста e1RM.

    Тот же источник, что у автопилота цели (goal/service.evaluate), — чтобы
    витрина и экран цели не расходились в оценке достижимого.

    `profile` (P1-03 ч.2, снижение стоимости витрины вех): необязательный
    уже загруженный профиль — build_showcase читает его сам, чтобы передать
    и сюда, и в evaluate() (см. её докстринг), вместо трёх независимых
    чтений одной и той же строки. Не передан — читаем сами, как раньше.
    """
    if profile is None:
        profile = (await session.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
        )).scalars().first()
    level = ((profile.experience_level if profile else None) or "beginner").strip().lower()
    return level, WEEKLY_GROWTH_CAP_PCT.get(level, _DEFAULT_CAP_PCT)
