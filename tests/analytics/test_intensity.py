from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import pytest
from api.services.analytics.load import normalize_set
from api.services.analytics.intensity import intensity_summary
from api.services.analytics.strength import session_points, record_history


def test_prepared_reference_reuses_admission_and_matches_independent_summaries(weighted_90_60, reference, monkeypatch):
    from api.services.analytics import intensity
    prepare = getattr(intensity, 'prepare_intensity', None)
    assert callable(prepare), 'Shared point/observation intensity must prepare historical candidates once'
    first = weighted_90_60[0]
    candidates = [*reference,
        replace(reference[0], set_id=20, finished_at=first.started_at),
        replace(reference[0], set_id=21, finished_at=first.started_at-timedelta(days=56, seconds=1)),
        replace(reference[0], set_id=22, workout_id=first.workout_id),
        replace(reference[0], set_id=23, is_deload=True),
        replace(reference[0], set_id=24, exercise_id=999)]
    windows = [weighted_90_60, [first], [replace(first,e1rm_reps=None)],
               [replace(first,set_type='drop')], [replace(first,exercise_id=998)]]
    expected = [intensity_summary(rows,candidates) for rows in windows]
    calls = 0
    original = intensity._e1rm
    def counted(row):
        nonlocal calls
        calls += 1
        return original(row)
    monkeypatch.setattr(intensity,'_e1rm',counted)
    prepared=prepare(candidates)
    initial_calls=calls
    for _ in range(10):
        for rows,want in zip(windows,expected):
            assert prepared.summary(rows)==want
    # Repeating detail/point requests must not re-admit and recompute every reference.
    assert calls-initial_calls <= sum(len(rows) for rows in windows)*10


def test_prepared_reference_deduplicates_only_exact_boundary_equivalent_candidates(weighted_90_60, reference):
    from api.services.analytics.intensity import prepare_intensity
    target=weighted_90_60[0]
    prior=reference[0]
    candidates=[replace(prior,set_id=i) for i in range(1000)]
    candidates += [replace(prior,workout_id=target.workout_id,set_id=1001),
                   replace(prior,finished_at=target.started_at,set_id=1002),
                   replace(prior,e1rm_weight_kg=prior.e1rm_weight_kg+1e-9,set_id=1003)]
    prepared=prepare_intensity(candidates)
    assert len(prepared.history[prior.exercise_id])==4
    assert prepared.summary(weighted_90_60)==intensity_summary(weighted_90_60,reference)


def test_repetition_weighted_intensity(weighted_90_60, reference):
    metric = intensity_summary(weighted_90_60, reference)
    assert metric.value == 66
    assert (metric.coverage.used, metric.coverage.total) == (2, 2)


def test_reference_before_start(weighted_90_60, reference):
    target = weighted_90_60[0]
    prior = reference[0]
    assert intensity_summary([target], [replace(prior, finished_at=target.started_at)]).value is None
    assert intensity_summary([target], [replace(prior, finished_at=target.started_at-timedelta(days=56))]).value == 90
    assert intensity_summary([target], [replace(prior, finished_at=target.started_at-timedelta(days=56, seconds=1))]).value is None
    for candidate in [replace(prior, workout_id=target.workout_id), replace(prior, is_deload=True),
                      replace(prior, exercise_id=999), replace(prior, finished_at=None)]:
        assert intensity_summary([target], [candidate]).value is None


def test_no_reference_or_drops_no_invented_zero(weighted_90_60, reference):
    assert intensity_summary(weighted_90_60, []).value is None
    drop = replace(weighted_90_60[0], set_type='drop')
    assert intensity_summary([drop], reference).coverage.total == 0
    unknown = replace(weighted_90_60[0], e1rm_reps=None)
    metric = intensity_summary([unknown, weighted_90_60[1]], reference)
    assert metric.value == 60
    assert (metric.coverage.used, metric.coverage.total) == (1, 2)


def test_normalized_load_not_dumbbell_tonnage_and_no_clamp(raw, reference):
    row = normalize_set(raw(weight_kg=Decimal('120'), reps=3, is_deload=True,
        load_context=dict(schema_version=1, model='dumbbell', input_basis='per_implement',
                          rep_basis='simultaneous', implement_count=2)))
    assert intensity_summary([row], reference).value == 120


def test_actual_reps_weight_side_total(raw, reference):
    row = normalize_set(raw(weight_kg=Decimal('90'), reps=6,
        load_context=dict(schema_version=1, model='dumbbell', input_basis='per_implement',
                          rep_basis='total', left_reps=3, right_reps=3)))
    other = normalize_set(raw(set_id=2, weight_kg=Decimal('60'), reps=12))
    assert intensity_summary([row, other], reference).value == 70


def test_period_invariance_and_upstream_gym_scope(weighted_90_60, reference):
    target = weighted_90_60[0]
    assert intensity_summary([target], reference).value == 90
    assert intensity_summary(weighted_90_60, reference).value == 66
    gym_a = replace(target, gym_id='a')
    gym_b = replace(reference[0], gym_id='b', e1rm_weight_kg=200)
    # Repository supplies the selected gym scope, which can include multiple gyms.
    all_rows = [gym_a, gym_b]
    scoped = [r for r in all_rows if r.gym_id == 'a']
    assert len(session_points(all_rows)) == 2 and len(session_points(scoped)) == 1
    assert record_history(all_rows)['best']['workout_id'] == gym_b.workout_id
    assert record_history(scoped)['best']['workout_id'] == gym_a.workout_id
    assert intensity_summary([gym_a], [gym_b]).value == 45
    assert intensity_summary([gym_a], []).value is None


def test_each_target_uses_own_historical_maximum(weighted_90_60, reference):
    first = weighted_90_60[0]
    later = replace(first, workout_id=3, set_id=4, started_at=first.started_at+timedelta(days=2),
                    finished_at=first.finished_at+timedelta(days=2))
    new_record = replace(reference[0], workout_id=4, set_id=5, e1rm_weight_kg=200,
                         finished_at=first.started_at+timedelta(days=1))
    candidates = [*reference, new_record]
    assert intensity_summary([first], candidates).value == 90
    assert intensity_summary([later], candidates).value == 45
    assert intensity_summary([first, later], candidates).value == 67.5


def test_reference_lombardi_uses_per_implement_reps(raw, weighted_90_60):
    target = weighted_90_60[0]
    prior = normalize_set(raw(workout_id=2, set_id=3, reps=16, weight_kg=Decimal('100'),
        finished_at=target.started_at-timedelta(seconds=1),
        load_context=dict(schema_version=1, model='dumbbell', input_basis='per_implement',
                          rep_basis='total', left_reps=8, right_reps=8)))
    assert intensity_summary([target], [prior]).value == pytest.approx(73.1027156720612)
