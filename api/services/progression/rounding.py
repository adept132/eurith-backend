"""Округление веса к шагу доступного оборудования.

Перенос из autoprogression.py плюс округление вниз: при снижении нагрузки
округление к ближайшему может вернуть исходный вес и не снизить ничего
(блочный тренажёр, шаг 10 lb на рабочих 30 кг).
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional

from api.services import equipment as equip
from api.services.progression.params import DEFAULT_WEIGHT_STEPS, LB_ROUNDING_TOLERANCE
from api.services.load_context import EffectiveLoadContext

# Физические константы перевода единиц — не настраиваемые пороги,
# поэтому в params.py не переносим.
LB_PER_KG = 2.2046226218
KG_PER_LB = 0.45359237
_KG_PER_LB_DECIMAL = Decimal("0.45359237")


def _kg(value: object, unit: str) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        return None
    if not number.is_finite() or number <= 0 or unit not in ("kg", "lb"):
        return None
    return number * (_KG_PER_LB_DECIMAL if unit == "lb" else Decimal(1))


def round_for_load_context(
    target_kg: float, context: EffectiveLoadContext, round_down: bool
) -> float | None:
    """Return a physically reachable recorded kg value for this machine setup.

    Plate counts are inventory counts, so two-sided loading consumes a pair.
    Decimal arithmetic keeps pound steps and fractional plates on their
    original grid. A named gym without inventory cannot invent plates.
    """
    target = _kg(target_kg, "kg")
    if target is None:
        return None

    if context.mode == "stack":
        step = _kg(context.step_value, context.step_unit)
        if step is None:
            return None
        multiple = target / step
        index = int(multiple.to_integral_value(rounding=ROUND_FLOOR if round_down else ROUND_HALF_UP))
        return float(step * index) if index > 0 else None

    if context.mode != "plate_loaded" or context.weight_basis not in (
        "plates_only", "including_start_weight"
    ) or context.loading_sides not in (1, 2) or not context.plates:
        return None

    base = Decimal(0)
    if context.weight_basis == "including_start_weight":
        if context.base_weight is None:
            return None
        try:
            base = Decimal(str(context.base_weight))
        except (ValueError, TypeError, InvalidOperation):
            return None
        if not base.is_finite() or base < 0:
            return None

    reachable = {Decimal(0)}
    for plate in context.plates:
        weight = _kg(plate.get("weight"), plate.get("unit", context.step_unit))
        count = plate.get("count")
        if weight is None or not isinstance(count, int) or isinstance(count, bool) or count < 0:
            continue
        available = count // context.loading_sides
        increment = weight * context.loading_sides
        # Values above this bound cannot be the closest reachable load.
        useful = max(0, int(((target - base) / increment).to_integral_value(rounding=ROUND_FLOOR)) + 1)
        available = min(available, useful)
        expanded = {total + increment * n for total in reachable for n in range(available + 1)}
        ceiling = target - base
        above = [value for value in expanded if value > ceiling]
        reachable = {value for value in expanded if value <= ceiling}
        if not round_down and above:
            reachable.add(min(above))

    candidates = [base + plates for plates in reachable if base + plates > 0]
    if not candidates:
        return None
    if round_down:
        candidates = [value for value in candidates if value <= target]
        return float(max(candidates)) if candidates else None
    return float(min(candidates, key=lambda value: (abs(value - target), value)))


def _resolve_step(category: str, unit: str, steps: Optional[dict]):
    """(шаг, единица шага) для категории оборудования."""
    s = {**DEFAULT_WEIGHT_STEPS, **(steps or {})}
    if category == equip.STEP_PLATE:
        return (s["plate_lb"], "lb") if unit == "lbs" else (s["plate_kg"], "kg")
    if category == equip.STEP_DUMBBELL:
        return (s["dumbbell_lb"], "lb") if unit == "lbs" else (s["dumbbell_kg"], "kg")
    if category == equip.STEP_BLOCK:
        return (s["block_lb"], "lb")
    # Прочее оборудование (bodyweight/band/...): тот же шаг, что и для
    # штанги/тренажёра со свободным весом — тоже должен уважать
    # пользовательское settings.weight_steps, а не жёстко зашитые 5/2.5.
    return (s["plate_lb"], "lb") if unit == "lbs" else (s["plate_kg"], "kg")


def _step_pair(equipment_needed, unit: str, steps: Optional[dict]):
    category = equip.equipment_to_step_category(equipment_needed)
    step, step_unit = _resolve_step(category, unit, steps)
    if not step or step <= 0:
        step, step_unit = (2.5, "kg")
    return step, step_unit


def step_kg(equipment_needed, unit: str = "kg", steps: Optional[dict] = None) -> float:
    """Размер шага в килограммах — для сравнений «в пределах одного шага»."""
    step, step_unit = _step_pair(equipment_needed, unit, steps)
    return round(step * KG_PER_LB, 4) if step_unit == "lb" else float(step)


def _apply(
    weight_kg: float, equipment_needed, unit, steps, round_down: bool
) -> float:
    """round_down — булев флаг, а не строка: опечатка не может тихо
    подменить режим округления к ближайшему."""
    step, step_unit = _step_pair(equipment_needed, unit, steps)

    if step_unit == "lb":
        lbs = weight_kg * LB_PER_KG
        if round_down:
            # Допуск на ошибку обратной конвертации: легитимная точка сетки
            # в фунтах, прошедшая через хранение в кг (round(..., 2)), может
            # вернуться в фунты чуть МЕНЬШЕ исходного значения — без допуска
            # floor съедает целый шаг (round_down_to_step перестаёт быть
            # идемпотентной). round_to_step сюда не попадает: там round, а
            # не floor, и такая ошибка направления не ломает ближайший шаг.
            rounded = math.floor(lbs / step + LB_ROUNDING_TOLERANCE / step) * step
        else:
            rounded = round(lbs / step) * step
        return round(max(step, rounded) * KG_PER_LB, 2)

    fn = math.floor if round_down else round
    rounded = fn(weight_kg / step) * step
    return round(max(step, rounded), 2)


def round_to_step(
    weight_kg: float, equipment_needed, unit: str = "kg", steps: Optional[dict] = None
) -> float:
    """К ближайшему шагу. Нижняя граница — один шаг."""
    return _apply(weight_kg, equipment_needed, unit, steps, round_down=False)


def round_down_to_step(
    weight_kg: float, equipment_needed, unit: str = "kg", steps: Optional[dict] = None
) -> float:
    """Вниз до шага. Нижняя граница — один шаг."""
    return _apply(weight_kg, equipment_needed, unit, steps, round_down=True)
