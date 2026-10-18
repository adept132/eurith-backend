"""Active comparison boundaries without connecting to a database."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from api.services.analytics import repository, service
from api.services.analytics.load import normalize_set
from api.services.analytics.types import AnalyticsQuery

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)  # Oct 16 in Moscow.


@pytest.fixture
def history(raw):
    blocks = [dict(id=1, block_index=1, status='closed', start_date=date(2026, 9, 1),
        actual_end_date=date(2026, 9, 7), planned_end_date=date(2026, 9, 7), phases=[]),
        dict(id=2, block_index=2, status='active', start_date=date(2026, 10, 13),
        actual_end_date=None, planned_end_date=date(2026, 11, 1), phases=[])]
    rows = []
    for block_id, days in ((1, (1, 2, 3, 4)), (2, (13, 14, 15, 16))):
        for index, day in enumerate(days):
            at = datetime(2026, 9 if block_id == 1 else 10, day - 1 if day == 16 else day,
                21 if day == 16 else 10, tzinfo=UTC)
            identity = len(rows) + 1
            rows.append(normalize_set(raw(workout_id=identity, set_id=identity,
                started_at=at - timedelta(hours=1), finished_at=at,
                weight_kg=Decimal(50 + index * block_id))))
    future = normalize_set(raw(workout_id=99, set_id=99,
        started_at=NOW, finished_at=NOW + timedelta(minutes=10)))
    rows.append(future)
    workouts = [dict(id=r.workout_id, status='finished', started_at=r.started_at,
        finished_at=r.finished_at) for r in rows]
    calendar = [dict(id=i, target_date=date(2026, 10, day), block_id=2,
        obligation_uuid=str(i), obligation_origin_date=date(2026, 10, day),
        obligation_snapshot={'block_id': 2}, is_rest_day=False, is_blackout=False,
        actual_workout_session_id=None) for i, day in enumerate(range(13, 19), 1)]
    return repository._HistorySnapshot(7, None, None, None,
        {'_timezone': 'Europe/Moscow', '_revision': 3}, blocks, workouts, calendar,
        rows, {r.workout_id: 1 if r.workout_id <= 4 else 2 for r in rows},
        [dict(exercise_id=101, main_muscle_group='chest', secondary_muscle_groups=[], name='Bench')], NOW)


def install_history(monkeypatch, history, measurements=()):
    async def load(*args):
        return history
    async def rows(*args):
        return list(measurements)
    monkeypatch.setattr(repository, '_load_history', load)
    monkeypatch.setattr(repository, '_rows', rows)


@pytest.mark.asyncio
async def test_active_and_closed_compare_observed_history_only(monkeypatch, history):
    install_history(monkeypatch, history)
    result = await service.compare_blocks(None, 7, 1, 2, None)
    assert result.b.block.status == 'active'
    assert result.b.block.actual_end_date is None
    assert result.b.scope.period.end == date(2026, 10, 16)
    assert result.a.computed_at == result.b.computed_at == NOW
    assert result.common_exercise_ids == [101]
    assert result.b.volume.work_sets == 4
    assert result.b.volume.calendar_days == 4
    assert result.b.adherence.planned_days == result.b.adherence.missed_days == 3
    assert all(point.at <= NOW for point in result.b.exercises[101].points)
    assert result.differences.muscles['chest'].value == pytest.approx(3.882741215298)
    assert result.differences.muscles['chest'].unit == 'percentage_points'


@pytest.mark.asyncio
async def test_two_started_active_blocks_compare_even_after_planned_end(monkeypatch, history):
    history.blocks[0].update(status='active', actual_end_date=None)
    history.blocks[1]['planned_end_date'] = date(2026, 10, 14)
    install_history(monkeypatch, history)
    result = await service.compare_blocks(None, 7, 1, 2, None)
    assert result.a.block.status == result.b.block.status == 'active'
    assert result.a.block.actual_end_date is result.b.block.actual_end_date is None
    assert result.a.scope.period.end == result.b.scope.period.end == date(2026, 10, 16)


@pytest.mark.asyncio
async def test_started_active_block_without_sessions_has_no_fabricated_growth(monkeypatch, history):
    install_history(monkeypatch, replace(history, facts=history.facts[:4], workouts=history.workouts[:4]))
    result = await service.compare_blocks(None, 7, 1, 2, None)
    assert result.b.volume.work_sets == 0
    assert result.b.exercises == {}
    assert result.common_exercise_ids == []
    assert all(value.value is None for value in result.differences.muscles.values())


@pytest.mark.asyncio
async def test_active_body_excludes_future_dates_and_later_recordings(monkeypatch, history):
    from api.services.models import BodyMeasurement
    def measurement(identity, day, recorded_at, value):
        row = {c.name: None for c in BodyMeasurement.__table__.columns}
        return dict(row, _source='body_measurement', id=identity, metric_key='weight',
            measured_on=day, recorded_at=recorded_at, value=value)
    rows = [measurement(1, date(2026, 10, 13), NOW - timedelta(days=3), 80),
        measurement(2, date(2026, 10, 15), NOW - timedelta(days=1), 81),
        measurement(3, date(2026, 10, 17), NOW - timedelta(days=1), 90),
        measurement(4, date(2026, 10, 16), NOW + timedelta(minutes=1), 91)]
    install_history(monkeypatch, history, rows)
    result = await service.compare_blocks(None, 7, 1, 2, None)
    assert result.b.body.metrics['weight'].after.id == 2
    assert result.b.body.metrics['weight'].delta == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status,start', [('planned', date(2026, 10, 13)),
    ('active', date(2026, 10, 17)), ('closed', date(2026, 10, 17))])
async def test_comparison_rejects_unstarted_blocks(monkeypatch, history, status, start):
    history.blocks[1].update(status=status, start_date=start)
    install_history(monkeypatch, history)
    with pytest.raises(ValueError, match='comparison_requires_started_blocks'):
        await service.compare_blocks(None, 7, 1, 2, None)


@pytest.mark.asyncio
async def test_comparison_rejects_identical_and_foreign_ids(monkeypatch, history):
    install_history(monkeypatch, history)
    with pytest.raises(ValueError, match='comparison_same_block'):
        await service.compare_blocks(None, 7, 2, 2, None)
    with pytest.raises(ValueError, match='block_not_found'):
        await service.compare_blocks(None, 7, 1, 999, None)


@pytest.mark.asyncio
async def test_unknown_closed_end_still_preserves_active_side(monkeypatch, history):
    history.blocks[0]['actual_end_date'] = None
    install_history(monkeypatch, history)
    result = await service.compare_blocks(None, 7, 1, 2, None)
    assert result.a.scope.reason == 'unknown_block_end'
    assert result.a.scope.period is None
    assert result.a.volume.work_sets == 4
    assert result.a.volume.work_sets_per_7_days is None
    assert result.b.scope.period.end == date(2026, 10, 16)
    assert result.common_exercise_ids == []


@pytest.mark.asyncio
async def test_active_scope_advances_at_profile_midnight_without_revision(monkeypatch, history):
    install_history(monkeypatch, replace(history, as_of=NOW - timedelta(hours=1)))
    before = await service.compare_blocks(None, 7, 1, 2, None)
    install_history(monkeypatch, history)
    after = await service.compare_blocks(None, 7, 1, 2, None)
    assert before.b.scope.period.end == date(2026, 10, 15)
    assert after.b.scope.period.end == date(2026, 10, 16)
    assert before.data_revision == after.data_revision


@pytest.mark.asyncio
async def test_comparison_does_not_subtract_recorded_and_normalized_load_bases(monkeypatch, history):
    recorded = dict(schema_version=1, model='recorded_load', input_basis='recorded',
                    source='legacy_recorded_weight')
    history.facts[:] = [normalize_set(replace(row, load_context=recorded))
                       if row.workout_id <= 4 else row for row in history.facts]
    install_history(monkeypatch, history)
    result = await service.compare_blocks(None, 7, 1, 2, None)
    assert result.a.volume.tonnage_per_7_days.value is not None
    assert result.b.volume.tonnage_per_7_days.value is not None
    assert result.differences.tonnage_per_7_days.value is None
    assert result.differences.tonnage_per_7_days.reason == 'incompatible_load_basis'
    assert result.common_exercise_ids == []
    assert result.differences.muscles['chest'].value is None
    assert any(item.reason == 'incompatible_load_basis' for item in result.excluded)
