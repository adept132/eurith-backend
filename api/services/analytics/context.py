"""Pure, gym-independent body and planned-obligation context."""
from collections import defaultdict
from datetime import date, timezone as utc_timezone
from math import isfinite
from zoneinfo import ZoneInfo

from .types import ResolvedPeriod


def select_boundary_measurement(rows: list[dict], metric: str, target: date, *, timezone: str) -> dict | None:
    """Select an explicit value, retaining its identity and date uncertainty."""
    candidates = []
    zone = ZoneInfo(timezone)
    for row in rows:
        if row.get('estimated') or row.get('value_estimated'):
            continue
        if row.get('source') == 'anthropometry' or 'submitted_fields' in row:
            if metric not in (row.get('submitted_fields') or []):
                continue
        if row.get('metric_key', metric) != metric:
            continue
        value = row.get('value', row.get(metric))
        if value is None or not isfinite(float(value)):
            continue
        recorded = row.get('recorded_at')
        if recorded is not None and recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=utc_timezone.utc)
        day = row.get('measured_on')
        source = 'measured_on'
        if day is None:
            day = recorded.astimezone(zone).date() if recorded else row.get('observation_date')
            source = 'recorded_at_local'
        if day is None or abs((day - target).days) > 7:
            continue
        result = dict(id=row.get('id'), source=row.get('source'), date=day,
                      value=float(value), date_source=source, date_estimated=source != 'measured_on')
        rank = (abs((day-target).days), day, -(recorded.timestamp() if recorded else 0), -(row.get('id') or 0), str(row.get('source', '')))
        candidates.append((rank, result))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def body_changes(rows: list[dict], period: ResolvedPeriod) -> dict:
    metrics = {}
    keys = {row['metric_key'] for row in rows if row.get('metric_key')}
    keys.update(key for row in rows for key in (row.get('submitted_fields') or []))
    for key in sorted(keys):
        first = select_boundary_measurement(rows, key, period.start, timezone=period.timezone)
        last = select_boundary_measurement(rows, key, period.end, timezone=period.timezone)
        if first is None and last is None:
            continue
        distinct = (first and last and first['date'] < last['date'] and
                    (first['source'], first['id']) != (last['source'], last['id']))
        metrics[key] = dict(before=first, after=last,
                            delta=last['value']-first['value'] if distinct else None)
    return dict(scope='block_context', metrics=metrics)


def adherence_summary(calendar: list[dict], workouts: list[dict], *, today: date,
                      period: ResolvedPeriod | None = None, is_closed: bool = False) -> dict:
    """Attribute each obligation to its latest proven target, retaining origin provenance."""
    zone = ZoneInfo(period.timezone if period else 'UTC')
    finished = {w['id']: w for w in workouts if w.get('status') == 'finished' and w.get('finished_at')}

    def in_period(day, block_id=None):
        return period is None or (period.start <= day <= period.end and
            (period.block_id is None or block_id == period.block_id))

    chains = defaultdict(list)
    for row in calendar:
        chains[str(row['obligation_uuid']) if row.get('obligation_uuid') else ('unknown', row['id'])].append(row)
    incomplete = not calendar
    if period:
        # Rest/blackout rows establish coverage too. An origin snapshot covers a
        # transferred date even when its previous calendar carrier was replaced.
        covered = set()
        for row in calendar:
            for day in (row['target_date'], row.get('obligation_origin_date')):
                if day and in_period(day, (row.get('obligation_snapshot') or {}).get('block_id', row.get('block_id'))):
                    covered.add(day)
        coverage_end = period.end if is_closed else min(period.end, today)
        incomplete = sum(d <= coverage_end for d in covered) < max(0, (coverage_end - period.start).days + 1)
    planned = completed = unresolved_completion_count = unresolved_due = 0
    observations, transferred_in, transferred_out = [], [], []
    linked_ids = {r.get('actual_workout_session_id') for r in calendar if r.get('actual_workout_session_id')}
    counted_workouts = set()
    for chain in chains.values():
        working = [r for r in chain if not r.get('is_rest_day') and not r.get('is_blackout')]
        if not working:
            continue
        first = min(working, key=lambda r: r.get('obligation_origin_date') or r['target_date'])
        snapshot = first.get('obligation_snapshot') or {}
        origin = first.get('obligation_origin_date') or first['target_date']
        block_id = snapshot.get('block_id', first.get('block_id'))
        known = all(r.get('obligation_uuid') and r.get('obligation_origin_date') and
                    r.get('obligation_snapshot') and not r['obligation_snapshot'].get('reconstructed') for r in working)
        targets = {r['target_date'] for r in working}
        current = [r for r in working if ((r.get('obligation_snapshot') or {}).get('transfer') or {}).get('is_current_target') is True]
        origins = {r.get('obligation_origin_date') for r in working}
        if len(origins) != 1 or (len(targets) > 1 and len(current) != 1):
            if any(in_period(r['target_date'], r.get('block_id')) or
                   in_period(r.get('obligation_origin_date') or r['target_date'], block_id) for r in working):
                incomplete = True
            continue
        target = current[0]['target_date'] if len(targets) > 1 else first['target_date']
        assigned_here = in_period(target, block_id)
        inferred = {r.get('actual_workout_session_id') for r in working
                    if ((r.get('obligation_snapshot') or {}).get('completion') or {}).get('source') == 'date_inferred'} & finished.keys()
        performed = sorted(({r.get('actual_workout_session_id') for r in working} & finished.keys()) - inferred)
        completion = finished[performed[0]]['finished_at'].astimezone(zone).date() if performed else None
        cancelled = any(isinstance((r.get('obligation_snapshot') or {}).get('cancellation'), dict) and
                        (r['obligation_snapshot']['cancellation'].get('operation') in
                         {'split_launch', 'close_block', 'same_block_regeneration', 'goal_undo'}) and
                        r['obligation_snapshot']['cancellation'].get('effective_on') and
                        r['obligation_snapshot']['cancellation'].get('recorded_at') for r in working)
        metadata = dict(obligation_uuid=str(first['obligation_uuid']) if first.get('obligation_uuid') else None,
                        origin_date=origin, target_date=target, block_id=block_id,
                        completed_on=completion, workout_ids=performed, unresolved_workout_ids=sorted(inferred), transferred=target != origin,
                        completed_outside_period=bool(completion and period and not period.start <= completion <= period.end))
        if not assigned_here:
            if known and in_period(origin, block_id):
                transferred_out.append(metadata)
            elif not known and any(in_period(r['target_date'], r.get('block_id')) for r in working):
                incomplete = True
            continue
        if cancelled:
            continue
        if inferred:
            incomplete = True
            unresolved_completion_count += 1
        if known and not in_period(origin, block_id):
            transferred_in.append(metadata)
        done = bool(performed)
        if not is_closed and (target > today or (target == today and not done)):
            continue
        if not known or any(wid in counted_workouts for wid in performed):
            incomplete = True
        counted_workouts.update(performed)
        planned += 1
        completed += int(done)
        unresolved_due += int(bool(inferred) and not done)
        observations.append(metadata)
    extras = sum(1 for wid, w in finished.items() if wid not in linked_ids and
                 (period is None or period.start <= w['finished_at'].astimezone(zone).date() <= period.end))
    return dict(scope='block_context', planned_days=planned, completed_days=completed,
                missed_days=planned-completed-unresolved_due, percent=round(100*completed/planned) if planned and not incomplete else None,
                reason='incomplete_calendar' if incomplete else None, obligations=observations,
                unresolved_completion_count=unresolved_completion_count,
                completion_reason='inferred_calendar_completion' if unresolved_completion_count else None,
                transferred_in=transferred_in, transferred_in_count=len(transferred_in),
                transferred_out=transferred_out, transferred_out_count=len(transferred_out), extra_workouts=extras)
