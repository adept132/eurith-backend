"""Сборка витрины вех и принятие вехи (P1-03 ч.2, §5.2, §5.4).

Срок берётся из симуляции движка прогрессии, а не из отдельной арифметики:
это ровно то число, которое автопилот потом будет защищать (решение 6).
Симуляция гоняется ТОЛЬКО для карточек, переживших фильтр видимости, — их не
больше шести, а сам фильтр обходится дешёвой арифметикой по потолку.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.goal.service import evaluate
from api.services.milestones import repository
from api.services.milestones.catalog import LIFTS, milestone_by_code, target_kg
from api.services.milestones.conditions import conditions_for
from api.services.milestones.visibility import (
    MAX_WEEKS_TO_TARGET, visible_milestones,
)
from api.services.models import (
    AppUserMesocycle, AppUserProfile, Mesocycle, PeriodizationProposal,
    UserExercisePreference, UserGoal,
)
from api.services.periodization import params as periodization_params
from api.services.structure import mesocycle_presets as meso_presets

GOAL_STRENGTH = "strength"


class LiftNotAvailable(Exception):
    """Упражнения движения нет в каталоге."""


class BodyweightUnknown(Exception):
    """Относительный порог посчитать нечем — вес тела не заполнен."""


@dataclass(frozen=True)
class MilestoneCard:
    code: str
    lift: str
    title: str
    target: Optional[float]
    remaining: Optional[float]
    target_reps: int
    has_history: bool
    suggested_deadline: Optional[date]
    accents: tuple[str, ...]
    split_requirement: dict
    mesocycle_preset: str


@dataclass(frozen=True)
class Showcase:
    cards: list[MilestoneCard]
    active_mesocycle_preset: Optional[str]


async def _active_mesocycle_code(
    session: AsyncSession, app_user_id: int
) -> Optional[str]:
    """Код пресета активного мезоцикла, если он из наших пресетов.

    Личные копии создаются из пресетов по имени (часть 1, §5.5), поэтому
    сверяем по имени: переименованная руками копия просто не опознается, и
    предложение не показывается — это лучше, чем угадывать.
    """
    row = (await session.execute(
        select(Mesocycle.name)
        .join(AppUserMesocycle, AppUserMesocycle.mesocycle_id == Mesocycle.id)
        .where(
            AppUserMesocycle.app_user_id == app_user_id,
            AppUserMesocycle.is_active.is_(True),
        )
        # Активная запись должна быть одна, но БД этого не гарантирует.
        # Без явного порядка выбор был бы недетерминирован, и подсказка
        # про мезоцикл прыгала бы между вызовами: берём последнюю.
        .order_by(AppUserMesocycle.id.desc())
        .limit(1)
    )).scalars().first()
    if row is None:
        return None
    for preset in meso_presets.MESOCYCLE_PRESETS:
        if preset.name == row:
            return preset.code
    return None


async def _simulated_deadline(
    session: AsyncSession, app_user_id: int, exercise_id: int,
    target: float, target_reps: int, today: date,
) -> Optional[date]:
    """Дата пересечения цели по симуляции, либо None.

    Цель собирается В ПАМЯТИ и не сохраняется: витрина обязана показать срок
    ДО того, как человек что-то принял. `evaluate` требует дедлайн только
    чтобы задать горизонт прокрутки, поэтому подставляем горизонт видимости —
    сам ответ (`eta`) от этой подстановки не зависит.
    """
    probe = UserGoal(
        app_user_id=app_user_id,
        goal_type=GOAL_STRENGTH,
        target_value=target,
        target_reps=target_reps,
        exercise_id=exercise_id,
        deadline=today + timedelta(weeks=MAX_WEEKS_TO_TARGET),
    )
    state = await evaluate(session, app_user_id, probe, today)
    return state["eta"] if state else None


async def build_showcase(
    session: AsyncSession, app_user_id: int, today: date
) -> Showcase:
    lift_exercises = await repository.resolve_lift_exercises(session)
    current = await repository.current_e1rm_by_lift(session, app_user_id, lift_exercises)
    bodyweight = await repository.latest_bodyweight(session, app_user_id)
    # Уровень нужен только чтобы получить потолок: сам по себе он на
    # видимость не влияет (правка по ревью Задачи 2).
    _level, cap_pct = await repository.experience_and_cap(session, app_user_id)

    cards: list[MilestoneCard] = []
    for v in visible_milestones(
        current_e1rm=current,
        bodyweight=bodyweight,
        ceiling_pct=cap_pct,
    ):
        lift = v.milestone.lift
        if lift not in lift_exercises:
            # Упражнения нет в каталоге — веху показывать не на чем.
            continue
        deadline = None
        if v.has_history and v.target is not None:
            deadline = await _simulated_deadline(
                session, app_user_id, lift_exercises[lift],
                v.target, v.milestone.target_reps, today,
            )
        cond = conditions_for(lift)
        cards.append(MilestoneCard(
            code=v.milestone.code,
            lift=lift,
            title=v.milestone.title,
            target=v.target,
            remaining=v.remaining,
            target_reps=v.milestone.target_reps,
            has_history=v.has_history,
            suggested_deadline=deadline,
            accents=cond.accents,
            split_requirement=cond.split_requirement,
            mesocycle_preset=cond.mesocycle_preset,
        ))
    return Showcase(
        cards=cards,
        active_mesocycle_preset=await _active_mesocycle_code(session, app_user_id),
    )


async def accept_milestone(
    session: AsyncSession, app_user_id: int, code: str, today: date
) -> UserGoal:
    """Превратить веху в ведущую цель. Не коммитит — это забота роутера.

    Порог считается ОДИН раз и кладётся в цель абсолютным числом: кратность
    своего веса — калькулятор витрины, а не свойство цели (решение 4).
    Плавающая цель убегала бы при наборе массы и достигалась бы сама собой на
    сушке.

    Схема прогрессии здесь НЕ перебивается (решение 7): вместо override
    рекомендуется мезоцикл из курируемой таблицы, и resolve_scheme сам
    переведёт тяжёлую штангу на percent_1rm в силовых фазах. Поднять
    override — работа автопилота, у которого для этого есть пересимуляция.
    """
    milestone = milestone_by_code(code)

    lift_exercises = await repository.resolve_lift_exercises(session)
    exercise_id = lift_exercises.get(milestone.lift)
    if exercise_id is None:
        raise LiftNotAvailable(milestone.lift)

    bodyweight = await repository.latest_bodyweight(session, app_user_id)
    target = target_kg(milestone, bodyweight)
    if target is None:
        raise BodyweightUnknown(code)

    # Срок — тем же движком симуляции, что и витрина (докстринг модуля):
    # то же число, которое автопилот потом будет защищать. Истории по
    # движению нет — evaluate() внутри вернёт None, и это не баг: срок не
    # выдумывается, состояние «у цели нет срока» система показывает честно.
    deadline = await _simulated_deadline(
        session, app_user_id, exercise_id, target, milestone.target_reps, today,
    )

    # Прежняя ведущая теряет флаг: одна ведущая цель на пользователя
    # (решение 2 спеки P0-12 — структурные рычаги не делятся между двумя
    # хозяевами). Сначала читаем её id, чтобы погасить её pending
    # goal_plan-предложения ТОЙ ЖЕ дисциплиной, что и
    # _retire_finished_primary_goal (goal/service.py) — иначе рычаг снятой
    # цели остаётся висеть и может быть применён к чужому упражнению.
    previous_primary_id = (await session.execute(
        select(UserGoal.id).where(
            UserGoal.app_user_id == app_user_id, UserGoal.is_primary.is_(True),
        )
    )).scalar_one_or_none()
    if previous_primary_id is not None:
        await session.execute(
            sa_update(PeriodizationProposal)
            .where(
                PeriodizationProposal.app_user_id == app_user_id,
                PeriodizationProposal.kind == periodization_params.KIND_GOAL_PLAN,
                PeriodizationProposal.status == periodization_params.STATUS_PENDING,
                PeriodizationProposal.payload["goal_id"].astext == str(previous_primary_id),
            )
            .values(status=periodization_params.STATUS_EXPIRED)
        )
    # Снятие флага — отдельным UPDATE (не через ORM-атрибут), чтобы гарантировать
    # порядок относительно частичного уникального индекса uq_user_goals_primary:
    # старая строка обязана уйти из-под индекса ДО insert новой (см. database.py).
    await session.execute(
        sa_update(UserGoal)
        .where(UserGoal.app_user_id == app_user_id, UserGoal.is_primary.is_(True))
        .values(is_primary=False)
    )

    goal = UserGoal(
        app_user_id=app_user_id,
        goal_type=GOAL_STRENGTH,
        target_value=target,
        target_reps=milestone.target_reps,
        exercise_id=exercise_id,
        is_primary=True,
        deadline=deadline,
    )
    session.add(goal)

    # Дешёвый рычаг: присутствие лифта в плане. Тот же носитель, что у
    # LEVER_ENSURE_PRESENT автопилота (P0-12 §5.2) — второго пути нет.
    existing_pref = (await session.execute(
        select(UserExercisePreference).where(
            UserExercisePreference.app_user_id == app_user_id,
            UserExercisePreference.exercise_id == exercise_id,
        )
    )).scalars().first()
    if existing_pref is None:
        session.add(UserExercisePreference(
            app_user_id=app_user_id,
            exercise_id=exercise_id,
            exercise_name=LIFTS[milestone.lift],
            preference="favorite",
        ))
    else:
        existing_pref.preference = "favorite"

    await session.flush()

    # Правило вехи, вес тела, id САМОЙ ЦЕЛИ на момент принятия (для строки
    # дрейфа §5.5) и условия под цель (§5.3). Флаш выше уже присвоил goal.id:
    # без него следующая ведущая цель (принятая не через веху, а обычным
    # PATCH /goals) могла бы получить чужой дрейф просто по совпадению
    # кода/веса. У UserGoal нет JSONB-поля, поэтому храним в settings
    # профиля рядом с progression.overrides; ведущая цель одна, одной записи
    # достаточно. Условия читает генерация (routers/plans.py) — §5.3
    # обещает, что они подставляются КАЖДЫЙ раз, когда план генерируется, а
    # не разово.
    cond = conditions_for(milestone.lift)
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalars().first()
    if profile is not None:
        settings = dict(profile.settings or {})
        settings["milestone"] = {
            "code": code,
            "bodyweight": bodyweight,
            "goal_id": goal.id,
            "accents": list(cond.accents),
            "split_requirement": cond.split_requirement,
        }
        profile.settings = settings

    return goal


# Расхождение веса тела, ниже которого строку не показываем: меньше — это
# вода и колебания дня, а не повод трогать поставленную цель (§5.5).
DRIFT_THRESHOLD = 0.05


@dataclass(frozen=True)
class BodyweightDrift:
    goal_id: int
    code: str
    title: str
    stored_bodyweight: float
    current_bodyweight: float
    current_target: float


async def bodyweight_drift(
    session: AsyncSession, app_user_id: int
) -> Optional[BodyweightDrift]:
    """Насколько уехал вес тела с момента принятия вехи.

    None во всех случаях, когда предлагать нечего:
    - веху не принимали;
    - веха абсолютная и от веса тела не зависит;
    - ведущей цели нет;
    - ведущая цель — не та, под которую принимали веху (id не совпадает —
      например, человек принял веху, а потом сделал ведущей свою цель
      через PATCH /goals/{id}: settings при этом не трогаются, и без
      сверки id строка дрейфа досталась бы чужой цели);
    - веху убрали из каталога;
    - вес тела не заполнен;
    - сохранённый вес нулевой или отрицательный (делить не на что);
    - расхождение меньше порога.

    Цель при этом НЕ правится молча — правку подтверждает человек через
    существующий PATCH /goals/{id} (решение 4: цель заморожена).
    """
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalars().first()
    stored = ((profile.settings or {}).get("milestone") or {}) if profile else {}
    code = stored.get("code")
    stored_bw = stored.get("bodyweight")
    stored_goal_id = stored.get("goal_id")
    if not code or stored_bw is None:
        return None

    try:
        milestone = milestone_by_code(code)
    except KeyError:
        # Веху убрали из каталога — строка теряет смысл, но экран не падает.
        return None
    if milestone.bodyweight_multiple is None:
        return None

    goal = (await session.execute(
        select(UserGoal).where(
            UserGoal.app_user_id == app_user_id,
            UserGoal.is_primary.is_(True),
        )
    )).scalars().first()
    # Записей без goal_id в проде нет (фича не выпущена) — отсутствие ключа
    # тоже трактуем как несовпадение, без обратной совместимости.
    if goal is None or goal.id != stored_goal_id:
        return None

    current_bw = await repository.latest_bodyweight(session, app_user_id)
    if current_bw is None or stored_bw <= 0:
        return None
    if abs(current_bw - stored_bw) / stored_bw < DRIFT_THRESHOLD:
        return None

    current_target = target_kg(milestone, current_bw)
    if current_target is None:
        return None

    return BodyweightDrift(
        goal_id=goal.id,
        code=code,
        title=milestone.title,
        stored_bodyweight=float(stored_bw),
        current_bodyweight=float(current_bw),
        current_target=current_target,
    )


def milestone_accents(profile_settings: Optional[dict]) -> list[str]:
    """Акценты принятой вехи из настроек профиля.

    Пустой список во всех случаях «вехи нет» — вызывающая сторона не обязана
    разбираться, чего именно не хватает.
    """
    milestone = ((profile_settings or {}).get("milestone") or {})
    accents = milestone.get("accents") or []
    return [str(a) for a in accents]
