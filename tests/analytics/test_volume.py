from dataclasses import replace

import pytest

from api.services.analytics.load import normalize_set
from api.services.analytics.types import Coverage
from api.services.analytics.volume import volume_summary


MUSCLES = {101: dict(main_muscle_group='chest', secondary_muscle_groups=['triceps'])}


def test_drop_separate_channels(raw):
    rows = [normalize_set(raw(set_id=i, set_type='drop' if i == 4 else 'normal'))
            for i in range(1, 5)]
    result = volume_summary(rows, MUSCLES, calendar_days=14)
    assert (result['work_sets'], result['drop_sets']) == (3, 1)
    assert result['tonnage'].value == 2000
    assert result['normal']['tonnage'].value == 1500
    assert result['drop']['tonnage'].value == 500
    assert result['muscles']['chest']['normal']['direct'] == 3
    assert result['muscles']['triceps']['normal']['effective'] == 1.5
    assert result['muscles']['chest']['drop']['direct'] == 1
    assert result['muscles']['triceps']['drop']['effective'] == .5
    assert result['muscles']['chest']['normal']['per_7_days']['direct'] == 1.5
    assert result['work_sets_per_7_days'] == 1.5


def test_partial_tonnage_coverage(raw):
    known = normalize_set(raw())
    unknown = replace(known, set_id=2, tonnage_kg=None, e1rm_weight_kg=None)
    result = volume_summary([known, unknown], {}, calendar_days=7)
    assert result['work_sets'] == 2
    assert result['tonnage'].value == 500
    assert result['tonnage'].coverage == Coverage(1, 2)
    assert result['coverage'] == Coverage(0, 2)
    assert result['tonnage_per_7_days'].coverage == Coverage(1, 2)
    assert volume_summary([unknown], {}, calendar_days=7)['tonnage'].value is None
    assert volume_summary([unknown], {}, calendar_days=7)['tonnage_per_7_days'].value is None
    assert volume_summary([], {}, calendar_days=7)['tonnage'].value == 0


def test_reuses_normalized_tonnage_and_keeps_side_ambiguous_counts(raw):
    row = normalize_set(raw(load_context=dict(schema_version=1, model='dumbbell',
        input_basis='per_implement', rep_basis='total', left_reps=4, right_reps=6)))
    assert row.e1rm_reps is None
    result = volume_summary([row], MUSCLES, calendar_days=3)
    assert result['work_sets'] == 1
    assert result['tonnage'].value == row.tonnage_kg == 500
    assert result['work_sets_per_7_days'] == 7 / 3


def test_paired_dumbbell_volume_is_not_converted_twice(raw):
    row = normalize_set(raw(load_context=dict(schema_version=1, model='dumbbell',
        input_basis='per_implement', rep_basis='simultaneous', implement_count=2)))
    result = volume_summary([row], MUSCLES, calendar_days=14)
    assert result['tonnage'].value == 1000
    assert result['tonnage_per_7_days'].value == 500
    assert result['normal']['tonnage_per_7_days'].value == 500
    assert result['drop']['tonnage_per_7_days'].value == 0


def test_duplicate_muscles_and_missing_mapping(raw):
    mapping = {101: dict(main_muscle_group='chest',
                         secondary_muscle_groups=['chest', 'triceps', 'triceps'])}
    rows = [normalize_set(raw()), normalize_set(raw(exercise_id=102, set_id=2))]
    result = volume_summary(rows, mapping, calendar_days=7)
    assert result['work_sets'] == 2
    assert result['coverage'] == Coverage(1, 2)
    assert result['muscles']['chest']['normal']['effective'] == 1
    assert result['muscles']['triceps']['normal']['effective'] == .5
    assert volume_summary(rows, {101: dict(main_muscle_group='unknown')}, calendar_days=7)['coverage'] == Coverage(0, 2)


@pytest.mark.parametrize('change', [dict(set_type='warmup'), dict(is_completed=False),
    dict(is_anomalous=True), dict(finished_at=None)])
def test_non_work_rows_excluded(raw, change):
    result = volume_summary([normalize_set(raw(**change))], MUSCLES, calendar_days=7)
    assert result['work_sets'] == result['drop_sets'] == 0
    assert result['muscles'] == {}


@pytest.mark.parametrize('days', [0, -1, 1.5, True, None])
def test_calendar_days_must_be_positive_integer(days):
    with pytest.raises(ValueError, match='calendar_days'):
        volume_summary([], {}, calendar_days=days)
