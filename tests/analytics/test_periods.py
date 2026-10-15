from datetime import date, timezone
import pytest
from api.services.analytics.types import AnalyticsQuery
from api.services.analytics.periods import resolve_period


def test_seven_days_includes_today():
    p = resolve_period(AnalyticsQuery(period='7'), timezone='UTC', today=date(2026, 10, 15), block=None)
    assert (p.start, p.end) == (date(2026, 10, 9), date(2026, 10, 15))


def test_custom_midnights_and_dst():
    p = resolve_period(AnalyticsQuery(period='custom', start=date(2026, 3, 29), end=date(2026, 3, 29)),
                       timezone='Europe/Berlin', today=date(2026, 10, 15), block=None)
    assert p.start_at.hour == p.end_exclusive.hour == 0
    assert p.end_exclusive.date() == date(2026, 3, 30)
    assert (p.end_exclusive.astimezone(timezone.utc) - p.start_at.astimezone(timezone.utc)).total_seconds() == 23 * 3600


def test_block_actual_end_only():
    q = AnalyticsQuery(period='block', block_id=3)
    b = dict(id=3, start_date=date(2026, 1, 1), actual_end_date=None,
             planned_end_date=date(2026, 2, 1), status='closed')
    with pytest.raises(ValueError, match='unknown_block_end'):
        resolve_period(q, timezone='UTC', today=date(2026, 10, 15), block=b)
    b['actual_end_date'] = date(2026, 1, 10)
    assert resolve_period(q, timezone='UTC', today=date(2026, 10, 15), block=b).end == date(2026, 1, 10)


@pytest.mark.parametrize('query', [AnalyticsQuery(period='bad'), AnalyticsQuery(period='custom'),
    AnalyticsQuery(period='custom', start=date(2026, 2, 1), end=date(2026, 1, 1))])
def test_invalid_period(query):
    with pytest.raises(ValueError):
        resolve_period(query, timezone='UTC', today=date(2026, 10, 15), block=None)


def test_all_empty_history_and_actual_start():
    today = date(2026, 10, 15)
    q = AnalyticsQuery(period='all')
    assert resolve_period(q, timezone='UTC', today=today, block=None).start == today
    assert resolve_period(q, timezone='UTC', today=today, block=None, history_start=date(2020,1,1)).start == date(2020,1,1)
    b = dict(id=3, start_date=date(2026,1,1), status='active', planned_end_date=date(2026,2,1))
    p = resolve_period(AnalyticsQuery(period='block',block_id=3), timezone='UTC',today=today,block=b)
    assert (p.start,p.end) == (b['start_date'],today)
