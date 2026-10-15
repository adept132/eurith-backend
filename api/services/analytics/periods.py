"""Resolve inclusive civil dates without assuming that a local day is 24 hours."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from .types import AnalyticsQuery, ResolvedPeriod


def validate_measurement_bounds(start: date, end: date) -> None:
    # Both +/-7 day windows and the exclusive upper midnight must fit.
    if start > end or start < date.min + timedelta(days=7) or end > date.max - timedelta(days=8):
        raise ValueError('invalid_period_bounds')


def resolve_period(query: AnalyticsQuery, *, timezone: str, today: date,
                   block: dict | None, history_start: date | None = None) -> ResolvedPeriod:
    zone = ZoneInfo(timezone)
    block_id = None
    if query.period in {'7', '30', '90', '365'}:
        start, end = today - timedelta(days=int(query.period)-1), today
    elif query.period == 'all':
        start, end = history_start or today, today
    elif query.period == 'custom':
        start, end = query.start, query.end
    elif query.period == 'block':
        if block is None or query.block_id != block.get('id'):
            raise ValueError('block_not_found')
        start = block.get('start_date')
        end = block.get('actual_end_date') if block.get('status') == 'closed' else today
        if end is None:
            raise ValueError('unknown_block_end')
        block_id = query.block_id
    else:
        raise ValueError('invalid_period')
    if start is None or end is None or start > end or end == date.max:
        raise ValueError('invalid_period_bounds')
    validate_measurement_bounds(start, end)
    return ResolvedPeriod(start=start, end=end, timezone=timezone,
        start_at=datetime.combine(start, time.min, zone),
        end_exclusive=datetime.combine(end + timedelta(days=1), time.min, zone), block_id=block_id)
