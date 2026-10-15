import pytest
from api.services.analytics.types import StrengthChange
from api.services.analytics.muscles import muscle_progress


def change(pct):
    return StrengthChange(100, 100+pct, pct, pct, '2+2', (1,2,3,4)) if pct is not None else StrengthChange(None,None,None,None,None,())


def test_equal_exercise_contribution():
    result, = muscle_progress({101:change(5),102:change(10),103:change(-3),104:change(None)}, {i:'chest' for i in range(101,105)})
    assert result.delta_pct == 4
    assert result.exercise_ids == (101,102,103)
    assert result.coverage.used == 3 and result.coverage.total == 4
    assert result.contributions[-1].excluded == ('insufficient_sessions',)


def test_common_set_before_averaging():
    a={101:change(99),102:change(3)}
    b={102:change(6),103:change(-90)}
    left, = muscle_progress(a, {i:'chest' for i in (101,102,103)}, allowed_ids={102})
    right, = muscle_progress(b, {i:'chest' for i in (101,102,103)}, allowed_ids={102})
    assert (left.delta_pct,right.delta_pct) == (3,6)
    assert right.delta_pct-left.delta_pct == 3
    assert left.contributions[0].excluded == ('not_common',)


def test_empty_common_is_not_zero():
    result, = muscle_progress({101:change(5)}, {101:'chest'}, allowed_ids=set())
    assert result.delta_pct is None and result.exercise_ids == ()
    assert 'no_common_exercises' in result.excluded
