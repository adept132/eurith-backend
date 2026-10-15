from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from api.services import anomaly_guard as guard
from api.services.analytics.load import normalize_set
from api.services.analytics.types import RawSetFact


def fact(weight=100, reps=10, **kwargs):
    raw = RawSetFact(4, 1, 1, 4, 1, 0, datetime.now(timezone.utc), None,
        None, None, None, 'normal', True, False, Decimal(weight), reps, None,
        {'schema_version': 1, 'model': 'barbell', 'input_basis': 'full',
         'rep_basis': 'simultaneous'}, None, False)
    return normalize_set(replace(raw, **kwargs))


@pytest.mark.parametrize('reps', [301, 1001])
def test_high_reps(reps):
    assert guard.check_set(20, reps, 'normal', None).level == 'ok'


@pytest.mark.parametrize('weight', [750, 2500])
def test_heavy_confirmable(weight):
    verdict = guard.check_set(weight, 8, 'normal', None)
    assert verdict.level == 'suspicious'
    assert not guard.resolve_is_anomalous(verdict, True)


def test_normalized_lombardi_and_comparable_history():
    assert hasattr(guard, 'check_analytics_set')
    current = fact(100, 1001)
    history = [replace(fact(100, 10), workout_id=i, started_at=current.started_at-timedelta(days=i+1)) for i in range(3)]
    assert 'e1rm_jump' in guard.check_analytics_set(current, history).reason_codes
    assert not guard.check_analytics_set(replace(current, comparison_key='other'), history).reason_codes
    assert 'e1rm_jump' not in guard.check_analytics_set(replace(current, e1rm_reps=10), history).reason_codes
    assert 'e1rm_jump' not in guard.check_analytics_set(replace(current, e1rm_weight_kg=None), history).reason_codes


@pytest.mark.parametrize('value', ['1.5', '-1', 'NaN', 'Infinity', '2147483648'])
def test_csv_rejects_invalid_actual_reps(value):
    from api.services.csv_import import parse_csv, CsvParseError
    with pytest.raises(CsvParseError):
        parse_csv('Date,Workout Name,Exercise Name,Set Order,Weight,Reps\n2026-08-20 18:00:00,W,E,1,20,' + value + '\n')


@pytest.mark.parametrize('field,value', [('reps', 1.5), ('reps', -1), ('reps', float('nan')),
    ('reps', 2147483648), ('weight', -1), ('weight', float('nan')),
    ('weight', float('inf')), ('weight', 1000000)])
def test_actual_input_formats(field, value):
    from pydantic import ValidationError
    from api.schemas.workouts import AddWorkoutSetRequest, UpdateWorkoutSetRequest
    from api.schemas.sync import SyncSetSnapshot
    from api.schemas.workout_history import WorkoutHistorySetDraft
    for model, base in [(AddWorkoutSetRequest, {}), (UpdateWorkoutSetRequest, {}),
            (SyncSetSnapshot, {'client_uuid': 'row', 'set_number': 1}),
            (WorkoutHistorySetDraft, {'client_uuid': 'row', 'set_number': 1})]:
        with pytest.raises(ValidationError):
            model.model_validate({**base, field: value})


@pytest.mark.parametrize('field,value', [('reps', 1001), ('weight', 2500)])
def test_ml_actual_numbers(field, value):
    from api.schemas.workout_import import MlWorkoutPatch
    item = MlWorkoutPatch.model_validate({'source_id': 'set', 'field': field, 'value': value,
        'confidence': 0.9, 'evidence_text': 'visible number', 'evidence': [
            {'image_index': 0, 'block_id': 'box', 'box': {'x': 0., 'y': 0., 'width': 1., 'height': 1.}}]})
    assert item.value == value


@pytest.mark.parametrize('pounds', ['1100000', '2204622.59'])
def test_csv_pounds_capacity_applies_after_conversion(pounds):
    from api.services.csv_import import parse_csv
    parsed = parse_csv('Date,Exercise Name,Set Order,Weight,Reps\n2026-08-20,E,1,' + pounds + ',8\n', default_unit='lbs')
    assert parsed.workouts[0].exercises[0].sets[0].weight_kg == round(float(pounds) * 0.45359237, 2)


@pytest.mark.parametrize('pounds', ['2204622.62', '2300000', 'NaN', 'Infinity', '-1', 'invalid'])
def test_csv_rejects_invalid_or_converted_overcapacity_pounds(pounds):
    from api.services.csv_import import parse_csv, CsvParseError
    with pytest.raises(CsvParseError, match='actual_weight_format'):
        parse_csv('Date,Exercise Name,Set Order,Weight,Reps\n2026-08-20,E,1,' + pounds + ',8\n', default_unit='lbs')
