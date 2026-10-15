from datetime import date, datetime, timezone
from decimal import Decimal
import pytest
from api.services.analytics.types import AnalyticsFacts, RawSetFact, ResolvedPeriod

@pytest.fixture
def raw():
    def make(**overrides):
        values = dict(workout_id=1, exercise_id=101, exercise_instance_id=1,
            set_id=1, set_number=1, exercise_order=1,
            started_at=datetime(2026, 10, 1, 10, tzinfo=timezone.utc),
            finished_at=datetime(2026, 10, 1, 11, tzinfo=timezone.utc),
            gym_id=None, gym_label=None, setup_id=None, set_type='normal',
            is_completed=True, is_anomalous=False, weight_kg=Decimal('50'),
            reps=10, effort_level=None, load_context={'schema_version': 1,
            'model': 'barbell', 'input_basis': 'full'}, session_context=None,
            is_deload=False)
        values.update(overrides)
        return RawSetFact(**values)
    return make

@pytest.fixture
def facts():
    def make(rows, **overrides):
        values = dict(period=ResolvedPeriod(date(2026,10,1), date(2026,10,1),
            'UTC', datetime(2026,10,1,tzinfo=timezone.utc),
            datetime(2026,10,2,tzinfo=timezone.utc), None), window_sets=list(rows),
            reference_sets=[], record_sets=[], calendar=[], measurements=[], blocks=[],
            source_revision=0)
        values.update(overrides)
        return AnalyticsFacts(**values)
    return make
