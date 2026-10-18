"""Recorded legacy scalars remain distinguishable from normalized resistance."""
from datetime import timedelta

import pytest

from api.services.analytics.load import normalize_set
from api.services.analytics.strength import session_points, strength_change, record_history
from api.services.analytics.intensity import intensity_summary
from api.services.analytics.volume import _tonnage
from api.services.analytics.legacy import recorded_load_context
from api.services.analytics.service import _exercise
from api.services.analytics.types import AnalyticsQuery

RECORDED = dict(schema_version=1, model='recorded_load', input_basis='recorded',
                source='legacy_recorded_weight')


def test_recorded_scalar_compatibility_keeps_honest_provenance(raw):
    row = normalize_set(raw(weight_kg=50, reps=10, load_context=RECORDED))
    assert row.e1rm_weight_kg == 50
    assert row.tonnage_kg == 500
    assert row.load_context == RECORDED
    assert row.comparison_key != normalize_set(raw()).comparison_key
    assert 'recorded_load_basis' in row.unavailable


def test_recorded_intensity_never_uses_normalized_reference(raw):
    previous = normalize_set(raw(workout_id=1, weight_kg=100, reps=1))
    target = normalize_set(raw(workout_id=2, weight_kg=50, reps=10,
        started_at=previous.started_at+timedelta(days=1),
        finished_at=previous.finished_at+timedelta(days=1), load_context=RECORDED))
    assert intensity_summary([target], [previous]).value is None
    compatible = normalize_set(raw(workout_id=1, weight_kg=100, reps=1, load_context=RECORDED))
    assert intensity_summary([target], [compatible]).value == 50


def test_strength_change_uses_latest_basis_without_cross_basis_comparison(raw):
    rows = [normalize_set(raw(workout_id=i, set_id=i, weight_kg=40+i, reps=1,
        started_at=raw().started_at+timedelta(days=i),
        finished_at=raw().finished_at+timedelta(days=i),
        load_context=RECORDED if i < 5 else raw().load_context)) for i in range(1, 6)]
    assert strength_change(session_points(rows[:4])).delta_pct is not None
    assert strength_change(session_points(rows)).delta_pct is None
    assert record_history(rows)['initial']['workout_id'] == 5
    assert record_history(rows)['events'] == []


def test_mixed_workout_and_tonnage_do_not_combine_incompatible_bases(raw):
    rows = [normalize_set(raw(set_id=1, load_context=RECORDED)),
            normalize_set(raw(set_id=2))]
    point = session_points(rows)[0]
    assert point.e1rm is None and point.volume is None
    assert 'incompatible_load_basis' in point.details.reasons
    assert _tonnage(rows).value is None


@pytest.mark.parametrize('change', [dict(analytics_snapshot={}),
    dict(saved_snapshot={}), dict(mode='plate_loaded'), dict(mode='stack'),
    dict(setup_id='known'), dict(defaults={}),
    dict(defaults={'model':'bodyweight','input_basis':'body_components'}),
    dict(analytics_snapshot={'schema_version':1,'model':'barbell','input_basis':'plates_only'}),
    dict(analytics_snapshot={'schema_version':1,'model':'dumbbell','input_basis':'per_implement'}),
    dict(analytics_snapshot={'schema_version':1,'model':'bodyweight','input_basis':'body_components'})])
def test_explicit_unknown_context_never_becomes_recorded_compatibility(change):
    values = dict(analytics_snapshot=None, saved_snapshot=None, mode=None,
                  setup_id=None, defaults=None)
    assert recorded_load_context(**values) == RECORDED
    assert recorded_load_context(**{**values, **change}) is None


def test_headline_best_uses_latest_basis_and_its_coverage(raw, facts):
    first = normalize_set(raw(workout_id=1, set_id=1, weight_kg=100, reps=1, load_context=RECORDED))
    last = normalize_set(raw(workout_id=2, set_id=2, weight_kg=60, reps=1,
        started_at=first.started_at+timedelta(days=1), finished_at=first.finished_at+timedelta(days=1)))
    rows = [first, last]
    resource = _exercise(facts(rows, record_sets=rows, graph_sets=rows), AnalyticsQuery(period='all'), 101)
    assert resource.summary.best_e1rm.value == 60
    assert resource.summary.best_e1rm.coverage.used == 1
    assert resource.summary.best_e1rm.coverage.total == 2
    assert [p.e1rm for p in resource.points] == [100, 60]


def test_recorded_provenance_is_informational_and_not_a_set_exclusion(raw, facts):
    row = normalize_set(raw(load_context=RECORDED))
    resource = _exercise(facts([row], record_sets=[row], graph_sets=[row]), AnalyticsQuery(period='all'), 101)
    assert resource.points[0].e1rm is not None
    assert resource.points[0].details.load_context == RECORDED
    assert 'recorded_load_basis' in resource.points[0].details.reasons
    assert all(item.reason != 'recorded_load_basis' for item in resource.excluded)
