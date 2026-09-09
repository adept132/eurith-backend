"""Курируемая таблица: движение -> условия под ведущую цель (§5.3).

То самое курирование, ради которого пункт бэклога существует: знание, которое
нельзя вывести из данных. Прикрепляется к ведущей цели и подставляется в
генератор КАЖДЫЙ раз, когда план генерируется; пока план не генерируется —
лежит без дела.

Требование к сплиту сужает выбор генератора, а не подменяет его: у человека
может быть три доступных дня, гантели вместо штанги и больная поясница, и
веха не вправе это перебить. Если ни один сплит требованию не удовлетворяет,
автоподбор снимает требование и объясняет причину (часть 1, §7).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LiftConditions:
    accents: tuple[str, ...]
    split_requirement: dict
    mesocycle_preset: str


CONDITIONS: dict[str, LiftConditions] = {
    "squat": LiftConditions(
        accents=("quads", "glutes", "hamstrings"),
        split_requirement={"any_of": ["legs", "lower", "full_body"], "min": 2},
        mesocycle_preset="strength",
    ),
    "bench": LiftConditions(
        accents=("chest", "triceps", "front_delts"),
        split_requirement={"any_of": ["push", "upper", "full_body"], "min": 2},
        mesocycle_preset="strength",
    ),
    # Разгибателей спины в таксономии проекта нет (§8.3), поэтому акцент под
    # становую неполон: перечисленное покрывает её частично.
    "deadlift": LiftConditions(
        accents=("hamstrings", "glutes", "lats", "traps"),
        split_requirement={"any_of": ["legs", "lower", "pull", "full_body"], "min": 2},
        mesocycle_preset="strength",
    ),
    "ohp": LiftConditions(
        accents=("front_delts", "side_delts", "triceps"),
        split_requirement={"any_of": ["push", "upper", "arms_shoulders", "full_body"], "min": 2},
        mesocycle_preset="strength",
    ),
    "pullup": LiftConditions(
        accents=("lats", "biceps", "mid_back"),
        split_requirement={"any_of": ["pull", "upper", "full_body"], "min": 2},
        mesocycle_preset="linear_4w",
    ),
    "row": LiftConditions(
        accents=("lats", "mid_back", "biceps", "rear_delts"),
        split_requirement={"any_of": ["pull", "upper", "full_body"], "min": 2},
        mesocycle_preset="strength",
    ),
}


def conditions_for(lift: str) -> LiftConditions:
    """Условия движения. Неизвестное движение — KeyError."""
    return CONDITIONS[lift]
