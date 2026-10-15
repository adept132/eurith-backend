"""Batched, read-only historical inputs, independent of progression's short window.

All set channels contain NormalizedSetFact (a RawSetFact subclass). Consumers use
its e1rm_reps and normalized loads, and must not apply load conversion again.
Sidecars retain owner-wide history for calendar chains and period comparisons.
"""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from sqlalchemy import select, or_, and_
from api.services.models import (AppUserProfile, AnalyticsState, WorkoutSession,
    WorkoutSessionExercise, WorkoutSessionSet, TrainingBlock, UserCalendarDay,
    UserAnthropometry, BodyMeasurement, Exercise)
from api.services.muscle_keys import to_system_key
from .types import AnalyticsQuery, AnalyticsFacts, RawSetFact
from .periods import resolve_period
from .load import normalize_set
from .legacy import legacy_load_context
from .policy import RULES_VERSION
from .snapshots import saved_phase_deload


async def _rows(db, statement):
    return [dict(row) for row in (await db.execute(statement)).mappings()]


def _deload(workout, blocks, days):
    if workout['analytics_snapshot'] is not None:
        value = workout['analytics_snapshot'].get('is_deload')
        return value if type(value) is bool else None
    block = blocks.get(workout['training_block_id'], {})
    flag = saved_phase_deload(block.get('phases'), workout['mesocycle_phase'])
    if flag is not None:
        return flag
    linked = days.get(workout['id'], [])
    if len(linked) == 1:
        day = linked[0]
        if ((workout['training_block_id'] is None or day['block_id'] == workout['training_block_id'])
                and (workout['mesocycle_phase'] is None or day['mesocycle_phase_number'] == workout['mesocycle_phase'])):
            return saved_phase_deload([dict(phase_number=1, effort_tier=day['meso_tag'])], 1)
    return None


async def load_facts(db, user_id: int, query: AnalyticsQuery, *,
                     exercise_ids: set[int] | None = None,
                     allow_unresolved_period: bool = False) -> AnalyticsFacts:
    # Scalar table projections deliberately avoid relationship loaders and writes.
    zone_name = await db.scalar(select(AppUserProfile.timezone).where(AppUserProfile.app_user_id == user_id)) or 'UTC'
    zone = ZoneInfo(zone_name)
    now = datetime.now(timezone.utc)
    today = now.astimezone(zone).date()
    revision = await db.scalar(select(AnalyticsState.source_revision).where(
        AnalyticsState.app_user_id == user_id, AnalyticsState.rules_version == RULES_VERSION)) or 0
    blocks = await _rows(db, select(TrainingBlock.__table__).where(TrainingBlock.app_user_id == user_id))
    block_map = {b['id']:b for b in blocks}
    if query.block_id is not None and query.block_id not in block_map:
        raise ValueError('block_not_found')
    preview = None
    if query.preview_workout_id is not None:
        preview = (await _rows(db, select(WorkoutSession.id, WorkoutSession.started_at, WorkoutSession.status).where(
            WorkoutSession.id == query.preview_workout_id, WorkoutSession.app_user_id == user_id)))
        if not preview or preview[0]['started_at'] is None:
            raise ValueError('preview_workout_not_found')
        preview = preview[0]
        if query.require_active_preview and preview['status'] != 'active':
            raise ValueError('preview_workout_not_active')
    workouts = await _rows(db, select(WorkoutSession.__table__).where(
        WorkoutSession.app_user_id == user_id, WorkoutSession.status == 'finished',
        WorkoutSession.finished_at.is_not(None)).order_by(WorkoutSession.finished_at, WorkoutSession.id))
    history_dates = [w['finished_at'].astimezone(zone).date() for w in workouts
                     if w['finished_at'].astimezone(zone).date() <= today]
    try:
        period = resolve_period(query, timezone=zone_name, today=today,
            block=block_map.get(query.block_id), history_start=min(history_dates, default=None))
    except ValueError as exc:
        if not allow_unresolved_period or str(exc) != 'unknown_block_end':
            raise
        period = None
    calendar = await _rows(db, select(UserCalendarDay.__table__).where(UserCalendarDay.app_user_id == user_id))
    linked_days = {}
    for day in calendar:
        if day['actual_workout_session_id'] is not None and not day['is_rest_day'] and not day['is_blackout']:
            linked_days.setdefault(day['actual_workout_session_id'], []).append(day)
    S, E, W = WorkoutSessionSet, WorkoutSessionExercise, WorkoutSession
    session_fields = ('started_at', 'finished_at', 'analytics_snapshot', 'gym_snapshot',
                      'training_block_id', 'mesocycle_phase')
    statement = select(S.__table__, E.exercise_id, E.id.label('exercise_instance_id'),
                       E.order_index.label('exercise_order'), W.id.label('workout_id'),
                       *(getattr(W, name).label('session_' + name) for name in session_fields)).join(
        E, S.workout_session_exercise_id == E.id).join(W, E.workout_session_id == W.id).where(
        W.app_user_id == user_id, W.status == 'finished', W.finished_at.is_not(None))
    if exercise_ids is not None:
        statement = statement.where(E.exercise_id.in_(exercise_ids))
    if query.gym_id == 'unknown':
        statement = statement.where(S.gym_profile_id.is_(None))
    elif query.gym_id is not None:
        statement = statement.where(S.gym_profile_id == query.gym_id)
    rows = await _rows(db, statement.order_by(W.finished_at, W.id, E.order_index, E.id, S.set_number, S.id))
    facts = []
    linked_block_workouts = set()
    for row in rows:
        # Context belongs to this joined observation, even if another session
        # finished between the independent global-workout and set batches.
        w = {name: row['session_' + name] for name in session_fields}
        w['id'] = row['workout_id']
        if w['training_block_id'] == query.block_id:
            linked_block_workouts.add(w['id'])
        saved_load = row['load_snapshot'] or {}
        context = row['analytics_load_snapshot']
        if context is None:
            context = legacy_load_context(saved_load, mode=row['load_mode'], gym_id=row['gym_profile_id'], setup_id=row['setup_id'])
        raw = RawSetFact(workout_id=w['id'], exercise_id=row['exercise_id'], exercise_instance_id=row['exercise_instance_id'],
            set_id=row['id'], set_number=row['set_number'], exercise_order=row['exercise_order'],
            started_at=w['started_at'], finished_at=w['finished_at'],
            gym_id=str(row['gym_profile_id']) if row['gym_profile_id'] is not None else None,
            gym_label=saved_load.get('gym_name') or (w['gym_snapshot'] or {}).get('name'),
            setup_id=str(row['setup_id']) if row['setup_id'] is not None else None,
            set_type=row['set_type'], is_completed=row['is_completed'], is_anomalous=row['is_anomalous'],
            weight_kg=Decimal(str(row['weight'])) if row['weight'] is not None else None,
            reps=row['reps'], effort_level=row['effort_level'], load_context=context,
            session_context=w['analytics_snapshot'], is_deload=_deload(w, block_map, linked_days))
        facts.append(normalize_set(raw))
    window = [f for f in facts if period.start_at <= f.finished_at < period.end_exclusive] if period else [
        f for f in facts if f.workout_id in linked_block_workouts]
    # Graph targets always retain full chronology, even during an active preview.
    targets = {f.workout_id:f.started_at for f in window}
    if preview:
        targets[preview['id']] = preview['started_at']
    reference = [f for f in facts if any(wid != f.workout_id and
        start - timedelta(days=56) <= f.finished_at < start
        for wid, start in targets.items())]
    cutoff = preview['started_at'] if preview else now
    records = [f for f in facts if f.workout_id != query.preview_workout_id
               and f.finished_at < cutoff]
    measurements = []
    try:
        measurement_start = (period.start if period else block_map[query.block_id]['start_date']) - timedelta(days=7)
        measurement_end = (period.end if period else block_map[query.block_id]['start_date']) + timedelta(days=7)
        # Validate UTC representability too: year-one local midnight in a
        # positive-offset zone otherwise fails inside the database driver.
        measurement_start_at = datetime.combine(measurement_start, time.min, zone).astimezone(timezone.utc)
        measurement_end_at = datetime.combine(measurement_end + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError('invalid_period_bounds') from exc
    for model, source in ((UserAnthropometry, 'anthropometry'), (BodyMeasurement, 'body_measurement')):
        for row in await _rows(db, select(model.__table__).where(model.app_user_id == user_id,
                or_(model.measured_on.between(measurement_start, measurement_end),
                    and_(model.measured_on.is_(None), model.recorded_at >= measurement_start_at,
                         model.recorded_at < measurement_end_at)))):
            day = row['measured_on'] or row['recorded_at'].astimezone(zone).date()
            common = dict(row, source=source, observation_date=day,
                          date_source='measured_on' if row['measured_on'] else 'recorded_at_local',
                          date_estimated=row['measured_on'] is None, estimated=False, value_estimated=False)
            if source == 'body_measurement':
                measurements.append(dict(common, value=Decimal(str(row['value']))))
            else:
                for key in ('weight', 'height', 'body_fat'):
                    if key in (row['submitted_fields'] or []) and row[key] is not None:
                        measurements.append(dict(common, metric_key=key, value=Decimal(str(row[key]))))
    catalog = await _rows(db, select(Exercise.id.label('exercise_id'), Exercise.main_muscle_group,
        Exercise.secondary_muscle_groups).where(or_(Exercise.app_user_id.is_(None), Exercise.app_user_id == user_id)))
    for row in catalog:
        row['main_muscle_group'] = to_system_key(row['main_muscle_group'])
        row['secondary_muscle_groups'] = sorted({key for item in (row['secondary_muscle_groups'] or [])
            if (key := to_system_key(item)) is not None and key != row['main_muscle_group']})
    return AnalyticsFacts(period=period, window_sets=window, reference_sets=reference, record_sets=records,
        calendar=calendar, measurements=measurements, blocks=blocks, source_revision=revision,
        catalog_mappings=catalog, global_workouts=workouts, timezone=zone_name,
        as_of=now, preview_start=preview["started_at"] if preview else None, graph_sets=facts)
