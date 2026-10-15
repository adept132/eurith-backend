from datetime import date, datetime, timezone
from decimal import Decimal

from api.services.analytics import context
from api.services.analytics.periods import resolve_period
from api.services.analytics.types import AnalyticsQuery


def period(start=1, end=20):
    return resolve_period(AnalyticsQuery(period='custom', start=date(2026, 8, start),
        end=date(2026, 8, end)), timezone='Europe/Moscow', today=date(2026, 8, 30), block=None)


def measurement(id, day, value, **kwargs):
    return dict(id=id, source='anthropometry', measured_on=date(2026, 8, day),
        metric_key='weight', value=Decimal(str(value)), submitted_fields=['weight'], **kwargs)


def test_boundary_tie_and_same_measurement():
    rows = [measurement(1, 3, 80), measurement(2, 17, 79)]
    assert context.select_boundary_measurement(rows, 'weight', date(2026, 8, 10), timezone='UTC')['id'] == 1
    assert context.select_boundary_measurement([rows[0]], 'weight', date(2026, 8, 11), timezone='UTC') is None
    result = context.body_changes([measurement(1, 10, 80)], period(5, 15))
    assert result['scope'] == 'block_context'
    assert result['metrics']['weight']['delta'] is None


def test_missing_calendar_and_carried_measurement():
    carried = measurement(1, 1, 80)
    carried['submitted_fields'] = ['height']
    assert context.select_boundary_measurement([carried], 'weight', date(2026, 8, 1), timezone='UTC') is None
    assert context.adherence_summary([], [], today=date(2026, 8, 30))['reason'] == 'incomplete_calendar'


def test_body_keeps_precision_and_source_identity():
    rows = [measurement(1, 1, '80.04'), measurement(1, 20, '80.06')]
    rows[1]['source'] = 'body_measurement'
    result = context.body_changes(rows, period())['metrics']['weight']
    assert abs(result['delta'] - .02) < 1e-10


def test_latest_on_day_and_metric_estimates():
    rows = [measurement(1, 1, 80, recorded_at=datetime(2026, 8, 1, 10, tzinfo=timezone.utc)),
            measurement(2, 1, 81, recorded_at=datetime(2026, 8, 1, 12, tzinfo=timezone.utc)),
            measurement(3, 1, 82, estimated=True)]
    assert context.select_boundary_measurement(rows, 'weight', date(2026, 8, 1), timezone='UTC')['id'] == 2
    rows[0]['recorded_at'] = rows[1]['recorded_at']
    assert context.select_boundary_measurement(rows[:2], 'weight', date(2026, 8, 1), timezone='UTC')['id'] == 2


def test_local_fallback_date_is_not_an_estimated_value():
    row = measurement(1, 1, 80, recorded_at=datetime(2026, 8, 1, 22, tzinfo=timezone.utc))
    row['measured_on'] = None
    found = context.select_boundary_measurement([row], 'weight', date(2026, 8, 2), timezone='Europe/Moscow')
    assert found['date'] == date(2026, 8, 2)
    assert found['date_source'] == 'recorded_at_local'
    assert found['date_estimated'] is True


def day(id, target, **kwargs):
    return dict(id=id, target_date=date(2026, 8, target), obligation_uuid=str(id),
        obligation_origin_date=date(2026, 8, target), obligation_snapshot={'source': 'schedule'},
        status='planned', **kwargs)


def test_transfer_once_and_extra_workout():
    rows = [day(i, i) for i in range(1, 25)]
    workouts = [dict(id=i, status='finished', finished_at=datetime(2026, 8, i, tzinfo=timezone.utc)) for i in range(1, 22)]
    for row in rows[:20]:
        row['actual_workout_session_id'] = row['id']
        row['status'] = 'completed'
    rows += [dict(rows[0], id=100, target_date=date(2026, 8, 25),
                  obligation_snapshot={'source': 'schedule', 'transfer': {'is_current_target': True}}),
             day(101, 26, is_rest_day=True), day(102, 27, is_blackout=True)]
    result = context.adherence_summary(rows, workouts, today=date(2026, 8, 30))
    assert (result['completed_days'], result['planned_days'], result['percent']) == (20, 24, 83)


def test_due_today_and_skipped():
    rows = [day(1, 1), day(2, 2), day(3, 3)]
    rows[0]['status'] = 'skipped'
    result = context.adherence_summary(rows, [], today=date(2026, 8, 2))
    assert result['planned_days'] == 1
    assert result['completed_days'] == 0
    closed = context.adherence_summary(rows, [], today=date(2026, 8, 2), period=period(1, 2), is_closed=True)
    assert closed['planned_days'] == 2


def test_transfer_attribution_across_adjacent_periods():
    original = day(1, 1)
    moved = dict(original, id=2, target_date=date(2026, 8, 20),
                 obligation_snapshot={'source': 'schedule', 'transfer': {'is_current_target': True}})
    early = period(1, 1)
    late = period(20, 20)
    pending = context.adherence_summary([original, moved], [], today=date(2026, 8, 10), period=early)
    assert pending['planned_days'] == 0
    moved['actual_workout_session_id'] = 10
    workouts = [dict(id=10, status='finished', finished_at=datetime(2026, 8, 21, tzinfo=timezone.utc))]
    fulfilled = context.adherence_summary([original, moved], workouts, today=date(2026, 8, 30), period=early, is_closed=True)
    assert fulfilled['planned_days'] == fulfilled['completed_days'] == 0
    assert fulfilled['transferred_out_count'] == 1
    incoming = context.adherence_summary([original, moved], workouts, today=date(2026, 8, 30), period=late, is_closed=True)
    assert incoming['planned_days'] == incoming['completed_days'] == 1
    assert incoming['obligations'][0]['completed_outside_period'] is True
    assert incoming['transferred_in_count'] == 1
    assert incoming['extra_workouts'] == 0


def test_incomplete_partial_calendar_is_not_a_trustworthy_zero():
    result = context.adherence_summary([day(1, 1)], [], today=date(2026, 8, 30), period=period(1, 3))
    assert result['reason'] == 'incomplete_calendar'
    assert result['percent'] is None


def test_transfer_order_needs_explicit_proof_and_can_move_earlier():
    original = day(1, 20)
    moved = dict(original, id=2, target_date=date(2026, 8, 1))
    args = dict(today=date(2026, 8, 30), period=period(1, 1), is_closed=True)
    unproven = context.adherence_summary([original, moved], [], **args)
    assert unproven['reason'] == 'incomplete_calendar'
    moved['obligation_snapshot'] = {'source': 'schedule', 'transfer': {'is_current_target': True}}
    proven = context.adherence_summary([original, moved], [], **args)
    assert proven['planned_days'] == 1
    assert proven['obligations'][0]['target_date'] == date(2026, 8, 1)
    original['obligation_snapshot'] = {'source': 'schedule', 'transfer': {'is_current_target': True}}
    assert context.adherence_summary([original, moved], [], **args)['reason'] == 'incomplete_calendar'


def test_date_inferred_attachment_is_not_proven_completion():
    row = day(1, 1, actual_workout_session_id=10)
    row['status'] = 'completed'
    row['obligation_snapshot']['completion'] = {'source': 'date_inferred', 'workout_session_id': 10}
    workouts = [dict(id=10, status='finished', finished_at=datetime(2026, 8, 1, tzinfo=timezone.utc))]
    result = context.adherence_summary([row], workouts, today=date(2026, 8, 2))
    assert result['percent'] is None
    assert result['completed_days'] == 0
    assert result['unresolved_completion_count'] == 1
    assert result['missed_days'] == 0
    assert result['reason'] == 'incomplete_calendar'
    row['obligation_snapshot']['completion']['source'] = 'explicit_calendar_intent'
    result = context.adherence_summary([row], workouts, today=date(2026, 8, 2))
    assert result['percent'] == 100
    assert result['completed_days'] == 1
