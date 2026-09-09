"""Курируемая таблица условий под ведущую цель (P1-03 ч.2, §5.3)."""
import pytest

from api.services.day_template import DayTemplateType
from api.services.milestones.catalog import LIFTS
from api.services.milestones.conditions import CONDITIONS, conditions_for
from api.services.structure.mesocycle_presets import MESOCYCLE_PRESETS
from api.services.volume_calculator import MUSCLE_TRANSLATION_MAP


def test_every_lift_has_conditions():
    assert set(CONDITIONS) == set(LIFTS)


def test_accents_are_real_system_muscle_keys():
    """Опечатка в ключе не упадёт здесь, но обнулит акцент в генераторе."""
    known = set(MUSCLE_TRANSLATION_MAP.values())
    for lift, cond in CONDITIONS.items():
        assert cond.accents, f"{lift}: акценты не должны быть пустыми"
        for key in cond.accents:
            assert key in known, f"{lift}: неизвестный ключ мышцы {key!r}"


def test_split_requirement_has_the_shape_the_autoselect_accepts():
    """Формат сверен с шагом 2 автоподбора (структура, часть 1, §5.2)."""
    day_types = {t.value for t in DayTemplateType}
    for lift, cond in CONDITIONS.items():
        req = cond.split_requirement
        assert set(req) == {"any_of", "min"}, f"{lift}: {req}"
        assert req["min"] >= 1
        assert req["any_of"], f"{lift}: пустой any_of"
        for day in req["any_of"]:
            assert day in day_types, f"{lift}: неизвестный тип дня {day!r}"


def test_mesocycle_presets_exist_in_the_catalog():
    codes = {p.code for p in MESOCYCLE_PRESETS}
    for lift, cond in CONDITIONS.items():
        assert cond.mesocycle_preset in codes, f"{lift}: {cond.mesocycle_preset}"


def test_barbell_lifts_get_the_strength_preset():
    """У пресета strength две силовые фазы вместо одной, то есть вдвое больше
    недель, где resolve_scheme переводит движение на проценты от максимума."""
    for lift in ("squat", "bench", "deadlift", "ohp", "row"):
        assert conditions_for(lift).mesocycle_preset == "strength"


def test_pullup_gets_a_linear_preset_not_the_strength_one():
    """Подтягиванию нужно МЕНЬШЕ недель на процентах, а не ноль.

    resolve_scheme переключает на percent_1rm по одному лишь fatigue_tier == 1
    (progression/resolve.py, слой 2), проверки снаряда там нет, а подтягивание
    как раз tier 1 — так что проценты к нему применяются. Но своим весом с
    блинами точное %1ПМ-планирование ведётся хуже, чем штангой, поэтому берём
    пресет с одной силовой фазой вместо двух."""
    assert conditions_for("pullup").mesocycle_preset != "strength"


def test_unknown_lift_raises():
    with pytest.raises(KeyError):
        conditions_for("нет такого движения")
