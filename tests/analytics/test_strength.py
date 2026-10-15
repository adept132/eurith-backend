from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import pytest
from api.services.analytics.load import normalize_set
from api.services.analytics.strength import session_points, strength_change, record_history


def test_3plus3_and_2plus2(points):
    change = strength_change(points([100, 102, 104, 106]))
    assert change.delta_kg == 4
    assert change.delta_pct == pytest.approx(400 / 101)
    assert change.method == '2+2'
    change = strength_change(points([100, 102, 200, 104, 106, 300]))
    assert (change.start_value, change.end_value, change.method) == (102, 106, '3+3')
    assert strength_change(points([100, 102, 104])).delta_kg is None
    assert strength_change(points([100, 102, 999, 104, 106])).workout_ids == (1, 2, 4, 5)


def test_deload_reduces_sample(points):
    assert strength_change(points([100, 102, 104, 106], deload_ids=(2,))).delta_kg is None


def test_repeated_instances_one_point_with_source_and_partial_volume(raw):
    rows = [normalize_set(raw(set_id=1, reps=1, weight_kg=Decimal('100'))),
            normalize_set(raw(set_id=2, exercise_instance_id=2, reps=1, weight_kg=Decimal('120'))),
            normalize_set(raw(set_id=3, set_type='drop', reps=2, weight_kg=Decimal('50')))]
    result, = session_points(rows[::-1])
    assert (result.e1rm, result.volume, result.work_sets, result.drop_sets) == (120, 320, 2, 1)
    assert result.source_set_id == 2
    assert result.details.load_context == rows[1].load_context
    assert result.details.coverage.used == 2


def test_pr_uses_preperiod_history(raw):
    first = normalize_set(raw(set_id=1, workout_id=1, reps=1, weight_kg=Decimal('100')))
    equal = replace(first, set_id=2, workout_id=2, finished_at=first.finished_at + timedelta(days=1))
    tiny = replace(equal, set_id=3, workout_id=3, e1rm_weight_kg=100.001,
                   finished_at=first.finished_at + timedelta(days=2), is_deload=True)
    history = record_history([tiny, equal, first])
    assert history['initial']['set_id'] == 1
    assert history['best']['set_id'] == 3
    assert [event['set_id'] for event in history['events']] == [3]
    assert history['events'][0]['previous_e1rm'] == 100
    assert record_history([first, equal])['best']['set_id'] == 1


@pytest.mark.parametrize('reps, band', [(1,'1-3'),(3,'1-3'),(4,'4-6'),(6,'4-6'),
    (7,'7-10'),(10,'7-10'),(11,'11-15'),(15,'11-15'),(16,'16+'),(1000001,'16+')])
def test_bands_lombardi_and_open_16(raw, reps, band):
    history = record_history([normalize_set(raw(reps=reps, weight_kg=Decimal('100')))])
    assert history['band'][band]['e1rm'] == pytest.approx(100 * reps ** .1)
    assert bool(history['weight_at_reps']) == (reps <= 12)


def test_side_counts_unknown_and_tonnage_independent(raw):
    ctx = dict(schema_version=1, model='dumbbell', input_basis='per_implement',
               rep_basis='total', left_reps=10, right_reps=10)
    row = normalize_set(raw(reps=20, weight_kg=Decimal('20'), load_context=ctx))
    history = record_history([row])
    assert history['best']['e1rm'] == pytest.approx(25.178508235883346)
    assert '7-10' in history['band']
    assert history['weight_at_reps']['10']['weight'] == 20
    assert history['set_volume']['value'] == 400
    asymmetric = normalize_set(raw(reps=20, load_context={**ctx, 'right_reps': 9}))
    point, = session_points([asymmetric])
    assert point.e1rm is None
    assert point.volume == 1000
    assert record_history([asymmetric])['best'] is None


def test_technical_tolerance_keeps_earliest_source(raw):
    row = normalize_set(raw(reps=1))
    other = replace(row, set_id=2, e1rm_weight_kg=50 + 1e-9)
    assert record_history([other, row])['best']['set_id'] == 1


def test_unavailable_not_zero_and_exclusions(raw):
    row = normalize_set(raw(load_context=None))
    point, = session_points([row])
    assert point.e1rm is None and point.volume is None
    assert point.work_sets == 1
    assert point.details.reasons
    assert session_points([replace(row, is_anomalous=True)]) == []
    assert record_history([])['initial'] is None


def test_chronology_ties_and_excluded_rows(raw):
    row = normalize_set(raw(reps=1, set_number=2, set_id=3))
    earlier = replace(row, set_number=1, set_id=2)
    excluded = [replace(row, set_id=4, e1rm_weight_kg=500, set_type='drop'),
                replace(row, set_id=5, e1rm_weight_kg=500, set_type='warmup'),
                replace(row, set_id=6, e1rm_weight_kg=500, is_completed=False),
                replace(row, set_id=7, e1rm_weight_kg=500, is_anomalous=True)]
    history = record_history([row, *excluded, earlier])
    assert history['initial']['set_id'] == 2
    assert history['best']['set_id'] == 2
    assert history['set_volume']['set_id'] == 2
    assert history['events'] == []


def test_unknown_tonnage_keeps_known_strength(raw):
    row = normalize_set(raw(reps=10, weight_kg=Decimal('20'),
        load_context=dict(schema_version=1, model='dumbbell', input_basis='per_implement',
                          rep_basis='simultaneous')))
    point, = session_points([row])
    assert point.e1rm == pytest.approx(25.178508235883346)
    assert point.volume is None
    assert record_history([row])['set_volume'] is None
