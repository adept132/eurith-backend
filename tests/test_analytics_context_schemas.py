from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from api.schemas.analytics_context import BodyMassSnapshot, SetLoadSnapshot, WorkoutAnalyticsSnapshot


def test_optional_context_roundtrip():
    value = WorkoutAnalyticsSnapshot()
    assert value.model_dump(mode='json') == {
        'schema_version': 1, 'body_mass': None, 'is_deload': None, 'phase_source': None,
    }
    assert WorkoutAnalyticsSnapshot.model_validate_json(value.model_dump_json()) == value


def test_known_zero_and_unknown():
    unknown = SetLoadSnapshot(model='bodyweight', input_basis='body_components')
    known = unknown.model_copy(update={'added_load_kg': 0, 'assistance_kg': 0})
    assert unknown.added_load_kg is None and known.added_load_kg == 0
    snapshot = BodyMassSnapshot(weight_kg=Decimal('70.5'), measured_on=date(2026, 10, 15),
                                source='imported', captured_at=datetime.now(timezone.utc))
    wire = snapshot.model_dump(mode='json')
    assert isinstance(wire['weight_kg'], (int, float))
    assert wire['measured_on'] == '2026-10-15'
    imported = BodyMassSnapshot.model_validate({**snapshot.model_dump(), 'import_provenance': {
        'original_source': 'journal', 'original_measurement_id': 'foreign-12',
        'import_format': 'full_csv',
    }})
    restored = BodyMassSnapshot.model_validate_json(imported.model_dump_json())
    assert restored.import_provenance.original_source == 'journal'
    assert restored.measurement_id is None
    numeric = SetLoadSnapshot(model='bodyweight', input_basis='body_components',
                              assistance_kg=Decimal('0.5')).model_dump(mode='json')
    assert isinstance(numeric['assistance_kg'], (int, float))
    for bad in [0, -1, float('inf'), float('nan')]:
        with pytest.raises(ValidationError):
            BodyMassSnapshot(weight_kg=bad, measured_on=date.today(), source='manual',
                             captured_at=datetime.now(timezone.utc))
