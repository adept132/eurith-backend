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

from api.services.milestones.catalog import MILESTONES, Milestone, target_kg

# Горизонт достижимости: веха, до которой при потолочном темпе идти дольше,
# не показывается вовсе — витрина не торгует недостижимым (§5.2, п. 2).
MAX_WEEKS_TO_TARGET = 104


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
    experience_level: str,
    ceiling_pct: float,
) -> list[VisibleMilestone]:
    """Ближайшая невзятая и достижимая веха по каждому движению.

    `current_e1rm` — рабочий e1RM по коду движения; отсутствие ключа значит
    «истории нет». Без истории отфильтровать недостижимое нечем, поэтому
    новичку открыта только нижняя ступень каждого движения (решение 10), а
    остальным нижняя ступень показывается без чисел и без срока (решение 9).
    """
    beginner = (experience_level or "").strip().lower() == "beginner"
    by_lift: dict[str, list[tuple[float, Milestone]]] = {}
    for m in MILESTONES:
        target = target_kg(m, bodyweight)
        if target is None:
            # Относительная веха при незаполненном весе тела не считается.
            continue
        by_lift.setdefault(m.lift, []).append((target, m))

    result: list[VisibleMilestone] = []
    for lift, rungs in by_lift.items():
        # Лестница задаётся килограммами, а не порядком объявления в каталоге.
        rungs.sort(key=lambda pair: (pair[0], pair[1].target_reps))
        current = current_e1rm.get(lift)

        if current is None:
            lowest_target, lowest = rungs[0]
            result.append(VisibleMilestone(lowest, lowest_target, None, False))
            continue

        if beginner:
            # У новичка история уже есть, но лестницу всё равно не открываем
            # целиком: пусть берёт ближайшую, а не цель на три года.
            rungs = rungs[:1] if rungs[0][0] > current else rungs

        for target, m in rungs:
            if current >= target:
                continue
            if not _reachable(current, target, ceiling_pct):
                break
            result.append(VisibleMilestone(m, target, round(target - current, 1), True))
            break

    # Порядок движений — как в каталоге, чтобы витрина не прыгала между вызовами.
    order = {m.lift: i for i, m in enumerate(MILESTONES)}
    result.sort(key=lambda v: order[v.milestone.lift])
    return result
