"""Какие вехи показывать (P1-03 ч.2, §5.2).

Чистая функция: БД не касается, значения приносит вызывающая сторона.

Достижимость решается арифметикой по биологическому потолку, а не симуляцией:
витрина — списочный экран, и гонять на нём полную прокрутку движка прогрессии
по шести движениям незачем. Симуляция считает СРОК уже показанным карточкам
(service.py) — это ровно то число, которое автопилот потом будет защищать.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from api.services.milestones.catalog import (
    MILESTONES, Milestone, target_e1rm, target_kg,
)

# Горизонт достижимости: веха, до которой при потолочном темпе идти дольше,
# не показывается вовсе — витрина не торгует недостижимым (§5.2, п. 2).
MAX_WEEKS_TO_TARGET = 104


def achieved_codes_from_sets(
    sets_by_lift: dict[str, list[tuple[Optional[float], int]]],
    bodyweight: Optional[float],
) -> set[str]:
    """Milestones actually completed in a finished normal set.

    The rolling e1RM can decline or be missing after an import. A past
    completed weight-and-rep result remains achieved. For pull-ups the logged
    weight is added resistance; unweighted reps need no weight entry.
    """
    achieved: set[str] = set()
    for milestone in MILESTONES:
        sets = sets_by_lift.get(milestone.lift, ())
        if milestone.lift == "pullup":
            extra = milestone.bodyweight_addend
            if any(
                reps >= milestone.target_reps
                and (extra == 0 or (weight is not None and weight >= extra))
                for weight, reps in sets
            ):
                achieved.add(milestone.code)
            continue
        target = target_kg(milestone, bodyweight)
        if target is not None and any(
            weight is not None and weight >= target and reps >= milestone.target_reps
            for weight, reps in sets
        ):
            achieved.add(milestone.code)
    return achieved


@dataclass(frozen=True)
class VisibleMilestone:
    milestone: Milestone
    target: Optional[float]
    remaining: Optional[float]
    has_history: bool


def _reachable(current: float, target: float, ceiling_pct: float) -> bool:
    """Успеет ли лифт дойти до порога за горизонт при потолочном темпе."""
    if current <= 0 or ceiling_pct <= 0:
        return False
    weekly = current * ceiling_pct
    return (target - current) / weekly <= MAX_WEEKS_TO_TARGET


def visible_milestones(
    *,
    current_e1rm: dict[str, float],
    bodyweight: Optional[float],
    ceiling_pct: float,
    achieved_codes: Optional[set[str]] = None,
) -> list[VisibleMilestone]:
    """Ближайшая невзятая и достижимая веха по каждому движению.

    `current_e1rm` — рабочий e1RM по коду движения; отсутствие ключа значит
    «истории нет». Без истории отфильтровать недостижимое нечем, поэтому
    нижняя ступень показывается без чисел и без срока, а сама эта ступень —
    и есть ближайшая невзятая (решение 9): для новичка это правило спеки
    «открыта только нижняя ступень» соблюдается само по себе, отдельного
    режима не нужно. Уровень опыта в этой функции не участвует — он уже учтён
    вызывающей стороной при вычислении `ceiling_pct`.

    Если для движения в `current_e1rm` лежит ровно 0.0 (а не отсутствующий
    ключ), оно пропадает из выдачи целиком: `_reachable` считает нулевой
    текущий вес недостижимым. Реальный e1RM нулевым не бывает, так что это
    вне контракта, но именно так ведёт себя код.

    Взятость, порядок лестницы и остаток считаются по ЦЕЛЕВОМУ e1RM
    (`target_e1rm`), а не по голым килограммам штанги (финальное ревью,
    Critical): `current_e1rm` — тоже e1RM, и сравнивать его с килограммами
    штанги неверно уже для одноповторных вех (Эпли даёт множитель 31/30, не
    1), а на многоповторных ошибка растягивается на десятки кг. `target` на
    карточке при этом остаётся килограммами штанги — это то, что человек
    реально будет поднимать (решение 5.1), меняется только то, ПО ЧЕМУ
    считается достижение цели.
    """
    by_lift: dict[str, list[tuple[float, float, Milestone]]] = {}
    achieved = achieved_codes or set()
    # П5 (финальное ревью, Important 5): подтягивания считаются числом
    # повторов, а не весом — свой вес используется как отягощение
    # автоматически, знать его точное значение не нужно, чтобы подтянуться.
    # Без веса тела target_e1rm для ВСЕХ ступеней подтягивания равен None
    # (все они относительные), но это не повод прятать движение целиком, как
    # прячутся остальные относительные вехи, — только числа на карточке.
    blind_pullup: list[Milestone] = []
    for m in MILESTONES:
        e1rm = target_e1rm(m, bodyweight)
        if e1rm is None:
            if m.lift == "pullup":
                blind_pullup.append(m)
            # Остальные относительные вехи при незаполненном весе тела
            # действительно нечем посчитать — не показываем (§7).
            continue
        kg = target_kg(m, bodyweight)
        by_lift.setdefault(m.lift, []).append((e1rm, kg, m))

    result: list[VisibleMilestone] = []
    for lift, rungs in by_lift.items():
        # Лестница задаётся целевым e1RM, а не порядком объявления в каталоге
        # и не голыми килограммами штанги.
        rungs.sort(key=lambda triple: (triple[0], triple[2].target_reps))
        current = current_e1rm.get(lift)

        if current is None:
            for _, kg, milestone in rungs:
                if milestone.code not in achieved:
                    result.append(VisibleMilestone(milestone, kg, None, False))
                    break
            continue

        for e1rm, kg, m in rungs:
            if m.code in achieved:
                continue
            if current >= e1rm:
                continue
            if not _reachable(current, e1rm, ceiling_pct):
                break
            result.append(VisibleMilestone(m, kg, round(e1rm - current, 1), True))
            break

    if blind_pullup:
        # Вес тела не заполнен вообще — по построению это ВСЕ ступени
        # подтягивания разом (target_e1rm для них либо все None, либо ни
        # одна), так что by_lift["pullup"] не заводится. Каталог уже несёт
        # подтягивания по возрастанию сложности — берём первую запись.
        for milestone in blind_pullup:
            if milestone.code not in achieved:
                result.append(VisibleMilestone(milestone, None, None, False))
                break

    # Порядок движений — как в каталоге, чтобы витрина не прыгала между вызовами.
    order = {m.lift: i for i, m in enumerate(MILESTONES)}
    result.sort(key=lambda v: order[v.milestone.lift])
    return result
