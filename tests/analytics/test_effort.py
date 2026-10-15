from dataclasses import replace
from decimal import Decimal

import pytest

from api.services.analytics.effort import effort_summary
from api.services.analytics.load import normalize_set
from api.services.analytics.types import Coverage


def test_missing_effort_is_not_default(raw):
    missing = normalize_set(raw())
    result = effort_summary([missing])
    assert result.mean_rir.value is None
    assert result.mean_rir.coverage == Coverage(0, 1)
    assert result.drift_rir.value is None
    rated = normalize_set(raw(set_id=2, effort_level='failure', reps=1))
    result = effort_summary([missing, rated])
    assert result.mean_rir.value == 0
    assert result.mean_rir.coverage == Coverage(1, 2)


def test_drift_group_and_period_median(raw):
    rows = []
    for workout, end in enumerate(['failure', 'prefailure', 'prefailure', 'medium'], 1):
        rows.extend(normalize_set(raw(workout_id=workout, set_id=workout*10+i,
                    set_number=i, effort_level=effort))
                    for i, effort in [(1, 'medium'), (2, end)])
    assert effort_summary(rows[:2]).drift_rir.value == -2
    result = effort_summary(list(reversed(rows)))
    assert result.drift_rir.value == -1
    assert result.drift_rir.coverage == Coverage(4, 4)


@pytest.mark.parametrize('change', [dict(weight_kg=Decimal('55')), dict(reps=9),
    dict(setup_id='other'), dict(load_context=dict(schema_version=1,
    model='bodyweight', input_basis='body_components', added_load_kg=0, assistance_kg=10))])
def test_changed_load_reps_assistance_or_setup_breaks_group(raw, change):
    rows = [normalize_set(raw(effort_level='medium')),
            normalize_set(raw(set_id=2, set_number=2, effort_level='failure', **change))]
    assert effort_summary(rows).drift_rir.value is None
    assert effort_summary(rows).drift_rir.coverage == Coverage(0, 1)


def test_group_then_pair_medians_and_explicit_order(raw):
    # One pair has two groups (-2, 0) => -1; another pair has -2.
    rows = [normalize_set(raw(exercise_order=order, set_number=number, set_id=i,
                weight_kg=Decimal(weight), effort_level=effort, workout_id=workout))
        for i, order, number, weight, effort, workout in [
            (8, 1, 1, '50', None, 1), (7, 1, 2, '50', 'medium', 1),
            (6, 2, 1, '50', 'failure', 1), (5, 3, 1, '60', 'medium', 1),
            (4, 3, 2, '60', 'medium', 1), (3, 3, 3, '50', None, 1),
            (2, 1, 1, '50', 'medium', 2), (1, 1, 2, '50', 'failure', 2)]]
    assert effort_summary(rows).drift_rir.value == -1.5
    # Another normal pair without a computable drift stays in the denominator.
    rows.append(normalize_set(raw(exercise_id=999)))
    assert effort_summary(rows).drift_rir.coverage == Coverage(2, 3)


def test_equal_set_weight_and_no_e1rm_requirement(raw):
    rows = [normalize_set(raw(effort_level='medium', reps=1)),
            normalize_set(raw(set_id=2, effort_level='failure', reps=100))]
    rows = [replace(row, e1rm_reps=None) for row in rows]
    assert effort_summary(rows).mean_rir.value == 1
    unknown = replace(rows[0], e1rm_weight_kg=None, tonnage_kg=None)
    assert effort_summary([unknown]).mean_rir.value == 2


def test_id_breaks_order_ties_and_missing_endpoint_is_skipped(raw):
    rows = [normalize_set(raw(set_id=i, effort_level=effort))
            for i, effort in [(4, None), (3, 'failure'), (1, None), (2, 'medium')]]
    assert effort_summary(rows).drift_rir.value == -2


def test_side_asymmetric_known_load_still_has_effort_and_drift(raw):
    context = dict(schema_version=1, model='dumbbell', input_basis='per_implement',
                   rep_basis='total', left_reps=4, right_reps=6)
    rows = [normalize_set(raw(set_id=i, set_number=i, effort_level=effort,
                            load_context=context))
            for i, effort in [(1, 'medium'), (2, 'failure')]]
    assert all(row.e1rm_reps is None for row in rows)
    assert effort_summary(rows).drift_rir.value == -2
    assert effort_summary(rows).mean_rir.value == 1


@pytest.mark.parametrize('change', [dict(set_type='drop'), dict(set_type='warmup'),
    dict(is_completed=False), dict(is_anomalous=True), dict(finished_at=None)])
def test_non_work_rows_excluded(raw, change):
    assert effort_summary([normalize_set(raw(effort_level='failure', **change))]).mean_rir.coverage == Coverage(0, 0)
