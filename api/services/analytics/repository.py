"""Batched, read-only historical inputs, independent of progression's short window.

All set channels contain NormalizedSetFact (a RawSetFact subclass). Consumers use
its e1rm_reps and normalized loads, and must not apply load conversion again.
Sidecars retain owner-wide history for calendar chains and period comparisons.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from sqlalchemy import select, or_, and_, literal, null, union_all
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


@dataclass(frozen=True)
class _HistorySnapshot:
    user_id: int
    gym_id: str | None
    exercise_ids: frozenset[int] | None
    preview_workout_id: int | None
    header: dict
    blocks: list[dict]
    workouts: list[dict]
    calendar: list[dict]
    facts: list
    block_membership: dict
    catalog: list[dict]
    as_of: datetime


async def _load_history(db, user_id, query, exercise_ids):
    # One owner-anchored metadata read retains native date/UUID types and
    # still returns a row when the owner has no blocks or preview. Scalar
    # subqueries avoid multiplying blocks by profile/state rows.
    owner = select(literal(user_id).label('id')).subquery()
    profile_zone = select(AppUserProfile.timezone).where(
        AppUserProfile.app_user_id == user_id).scalar_subquery()
    source_revision = select(AnalyticsState.source_revision).where(
        AnalyticsState.app_user_id == user_id,
        AnalyticsState.rules_version == RULES_VERSION).scalar_subquery()
    metadata = await _rows(db, select(TrainingBlock.__table__,
        profile_zone.label('_timezone'), source_revision.label('_revision'),
        WorkoutSession.id.label('_preview_id'), WorkoutSession.started_at.label('_preview_start'),
        WorkoutSession.status.label('_preview_status')).select_from(owner.outerjoin(
            TrainingBlock, TrainingBlock.app_user_id == owner.c.id).outerjoin(
            WorkoutSession, and_(WorkoutSession.app_user_id == owner.c.id,
                                 WorkoutSession.id == query.preview_workout_id))))
    header = metadata[0]
    blocks = [{column.name: row[column.name] for column in TrainingBlock.__table__.columns}
              for row in metadata if row['id'] is not None]
    block_map = {b['id']: b for b in blocks}
    now = datetime.now(timezone.utc)
    workouts = await _rows(db, select(WorkoutSession.__table__).where(
        WorkoutSession.app_user_id == user_id, WorkoutSession.status == 'finished',
        WorkoutSession.finished_at.is_not(None)).order_by(WorkoutSession.finished_at, WorkoutSession.id))
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
    comparison_cache = {}
    block_membership = {}
    for row in rows:
        # Context belongs to this joined observation, even if another session
        # finished between the independent global-workout and set batches.
        w = {name: row['session_' + name] for name in session_fields}
        w['id'] = row['workout_id']
        block_membership[w['id']] = w['training_block_id']
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
        facts.append(normalize_set(raw, comparison_cache=comparison_cache))
    catalog = await _rows(db, select(Exercise.id.label('exercise_id'), Exercise.name, Exercise.main_muscle_group,
        Exercise.secondary_muscle_groups).where(or_(Exercise.app_user_id.is_(None), Exercise.app_user_id == user_id)))
    for row in catalog:
        row['main_muscle_group'] = to_system_key(row['main_muscle_group'])
        row['secondary_muscle_groups'] = sorted({key for item in (row['secondary_muscle_groups'] or [])
            if (key := to_system_key(item)) is not None and key != row['main_muscle_group']})
    return _HistorySnapshot(user_id, query.gym_id,
        frozenset(exercise_ids) if exercise_ids is not None else None,
        query.preview_workout_id, header, blocks, workouts, calendar, facts,
        block_membership, catalog, now)


async def _project_facts(db, history, query, *, allow_unresolved_period=False):
    if query.gym_id != history.gym_id or query.preview_workout_id != history.preview_workout_id:
        raise ValueError('history_scope_mismatch')
    user_id = history.user_id
    workouts, calendar, facts, catalog = history.workouts, history.calendar, history.facts, history.catalog
    linked_block_workouts = {wid for wid, bid in history.block_membership.items() if bid == query.block_id}
    header = history.header
    zone_name = header['_timezone'] or 'UTC'
    zone = ZoneInfo(zone_name)
    now = history.as_of
    today = now.astimezone(zone).date()
    revision = header['_revision'] or 0
    blocks = history.blocks
    block_map = {b['id']:b for b in blocks}
    if query.block_id is not None and query.block_id not in block_map:
        raise ValueError('block_not_found')
    active_block = query.period == 'block' and block_map[query.block_id]['status'] == 'active'
    if active_block:
        # The period still describes inclusive local days, but observations must
        # stop at this request's captured instant, including later on that day.
        facts = [f for f in facts if f.finished_at <= now]
        workouts = [w for w in workouts if w['finished_at'] <= now]
    preview = None
    if query.preview_workout_id is not None:
        if header['_preview_id'] is None or header['_preview_start'] is None:
            raise ValueError('preview_workout_not_found')
        if query.require_active_preview and header['_preview_status'] != 'active':
            raise ValueError('preview_workout_not_active')
        preview = dict(id=header['_preview_id'], started_at=header['_preview_start'])
    history_dates = [w['finished_at'].astimezone(zone).date() for w in workouts
                     if w['finished_at'].astimezone(zone).date() <= today]
    try:
        period = resolve_period(query, timezone=zone_name, today=today,
            block=block_map.get(query.block_id), history_start=min(history_dates, default=None))
    except ValueError as exc:
        if not allow_unresolved_period or str(exc) != 'unknown_block_end':
            raise
        period = None
    window = [f for f in facts if period.start_at <= f.finished_at < period.end_exclusive] if period else [
        f for f in facts if f.workout_id in linked_block_workouts]
    # Graph targets always retain full chronology, even during an active preview.
    targets = {f.workout_id:f.started_at for f in window}
    if preview:
        targets[preview['id']] = preview['started_at']
    target_windows = [(wid, start - timedelta(days=56), start) for wid, start in targets.items()]
    reference = [f for f in facts if any(wid != f.workout_id and
        lower <= f.finished_at < start for wid, lower, start in target_windows)]
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
    # UNION ALL batches independent measurement sources without a join that
    # could multiply rows. Each source keeps its original column shape below;
    # notably body measurements must not acquire submitted_fields semantics.
    measurement_models = {'anthropometry': UserAnthropometry, 'body_measurement': BodyMeasurement}
    column_names = sorted({c.name for model in measurement_models.values() for c in model.__table__.columns})
    selections = []
    for source, model in measurement_models.items():
        selections.append(select(literal(source).label('_source'), *(
            (model.__table__.c[name] if name in model.__table__.c else null()).label(name)
            for name in column_names)).where(model.app_user_id == user_id,
                or_(model.measured_on.between(measurement_start, measurement_end),
                    and_(model.measured_on.is_(None), model.recorded_at >= measurement_start_at,
                         model.recorded_at < measurement_end_at))))
    for combined in await _rows(db, union_all(*selections)):
        source = combined['_source']
        row = {c.name: combined[c.name] for c in measurement_models[source].__table__.columns}
        day = row['measured_on'] or row['recorded_at'].astimezone(zone).date()
        if active_block and (day > today or row['recorded_at'] > now):
            continue
        common = dict(row, source=source, observation_date=day,
                      date_source='measured_on' if row['measured_on'] else 'recorded_at_local',
                      date_estimated=row['measured_on'] is None, estimated=False, value_estimated=False)
        if source == 'body_measurement':
            measurements.append(dict(common, value=Decimal(str(row['value']))))
        else:
            for key in ('weight', 'height', 'body_fat'):
                if key in (row['submitted_fields'] or []) and row[key] is not None:
                    measurements.append(dict(common, metric_key=key, value=Decimal(str(row[key]))))
    return AnalyticsFacts(period=period, window_sets=window, reference_sets=reference, record_sets=records,
        calendar=calendar, measurements=measurements, blocks=blocks, source_revision=revision,
        catalog_mappings=catalog, global_workouts=workouts, timezone=zone_name,
        as_of=now, preview_start=preview["started_at"] if preview else None, graph_sets=facts)


async def load_facts(db, user_id: int, query: AnalyticsQuery, *,
                     exercise_ids: set[int] | None = None,
                     allow_unresolved_period: bool = False) -> AnalyticsFacts:
    history = await _load_history(db, user_id, query, exercise_ids)
    return await _project_facts(db, history, query, allow_unresolved_period=allow_unresolved_period)


async def load_comparison_facts(db, user_id: int, qa: AnalyticsQuery, qb: AnalyticsQuery):
    """One request-owned joined history, independently projected block windows."""
    if (qa.period != 'block' or qb.period != 'block' or qa.gym_id != qb.gym_id
            or qa.preview_workout_id is not None or qb.preview_workout_id is not None):
        raise ValueError('comparison_history_scope_mismatch')
    if qa.block_id == qb.block_id:
        raise ValueError('comparison_same_block')
    history = await _load_history(db, user_id, qa, None)
    blocks = {b['id']: b for b in history.blocks}
    # Resolve ownership for both sides before reporting eligibility. Future
    # materialized blocks are plans, even when their stored status is active.
    if qa.block_id not in blocks or qb.block_id not in blocks:
        raise ValueError('block_not_found')
    today = history.as_of.astimezone(ZoneInfo(history.header['_timezone'] or 'UTC')).date()
    if any(blocks[bid]['status'] not in {'active', 'closed'} or blocks[bid]['start_date'] > today
           for bid in (qa.block_id, qb.block_id)):
        raise ValueError('comparison_requires_started_blocks')
    a = await _project_facts(db, history, qa, allow_unresolved_period=True)
    b = await _project_facts(db, history, qb, allow_unresolved_period=True)
    return a, b
