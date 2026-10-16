"""Read-only assembly of independent analytics kernels and their provenance."""
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from api.schemas.analytics import (AnalyticsScope, ExerciseAnalytics, ExercisePoint, ExerciseSummary,
    RecordBaseline, MuscleAnalytics, BlockAnalytics, BlockComparison, BlockMetadata,
    BodyContext, AdherenceContext, ComparisonDifferences, Difference, Exclusion, VolumeSummary,
    MuscleProgress, MuscleContribution)
from .types import AnalyticsQuery, AnalyticsFacts, Coverage, Metric, StrengthChange
from .repository import load_facts
from .strength import session_points, strength_change, record_history
from .intensity import intensity_summary, prepare_intensity
from .effort import effort_summary
from .volume import volume_summary, _tonnage
from .muscles import muscle_progress
from .context import body_changes, select_boundary_measurement, adherence_summary
from .policy import eligible


def _gym_scope(gym_id):
    return 'all' if gym_id is None else 'unknown' if gym_id == 'unknown' else f'gym:{gym_id}'


def _envelope(facts: AnalyticsFacts, query: AnalyticsQuery, exercise_id: int | None = None):
    return dict(data_revision=facts.source_revision, computed_at=facts.as_of or datetime.now(timezone.utc),
        scope=AnalyticsScope(period=facts.period, timezone=facts.timezone, gym_id=query.gym_id,
            gym_scope=_gym_scope(query.gym_id), exercise_id=exercise_id, block_id=query.block_id,
            preview_workout_id=query.preview_workout_id,
            reason=None if facts.period else 'unknown_block_end'))


def _volume(facts, rows):
    mappings = {m['exercise_id']: m for m in facts.catalog_mappings}
    days = (facts.period.end - facts.period.start).days + 1 if facts.period else None
    # The kernel requires a divisor. For unresolved periods retain only its raw
    # counts/tonnage and replace every derived rate before exposing the result.
    result = volume_summary(rows, mappings, calendar_days=days or 1)
    if days is None:
        reason = 'unknown_block_end'
        result.update(calendar_days=None, reason=reason, work_sets_per_7_days=None,
            drop_sets_per_7_days=None, tonnage_per_7_days=Metric(None, reason))
        for name in ('normal', 'drop'):
            result[name].update(sets_per_7_days=None, tonnage_per_7_days=Metric(None, reason))
        for channels in result['muscles'].values():
            for channel in channels.values():
                channel['per_7_days'] = {key: None for key in channel['per_7_days']}
    return VolumeSummary(**result)


def _points(facts, rows, prepared_intensity, pure_points):
    groups = defaultdict(list)
    zone = ZoneInfo(facts.timezone)
    for row in rows:
        if eligible(row, 'work_sets') or eligible(row, 'drop_sets'):
            groups[row.workout_id, row.exercise_id].append(row)
    result = []
    for point in pure_points:
        group = groups[point.workout_id, point.exercise_id]
        intensity = prepared_intensity.summary(group)
        effort = effort_summary(group).mean_rir
        # Point details need only tonnage/coverage, not a second full catalog
        # muscle-volume fold. Preserve the kernel's normal-then-drop sum order.
        volume = _tonnage([row for row in group if row.set_type == 'normal']
                          + [row for row in group if row.set_type == 'drop'])
        observations = []
        for row in group:
            observations.append(dict(set_id=row.set_id, exercise_instance_id=row.exercise_instance_id,
                set_number=row.set_number, setup_id=row.setup_id, gym_id=row.gym_id, gym_label=row.gym_label,
                at=row.finished_at, observation_date=row.finished_at.astimezone(zone).date(),
                set_type=row.set_type, actual_reps=row.reps, e1rm_reps=row.e1rm_reps,
                e1rm_weight_kg=row.e1rm_weight_kg, tonnage_kg=row.tonnage_kg, rir=row.rir,
                intensity=prepared_intensity.summary([row]), load_context=row.load_context,
                session_context=row.session_context, reasons=row.unavailable))
        detail = asdict(point.details)
        detail.update(intensity=intensity, effort=effort, observations=observations,
            metric_coverage=dict(strength=point.details.coverage, volume=volume.coverage,
                intensity=intensity.coverage, effort=effort.coverage),
            observation_dates=tuple(sorted({r.finished_at.astimezone(zone).date() for r in group})))
        result.append(ExercisePoint(**{**asdict(point), 'details': detail}))
    return result


def _exclusions(rows):
    result = []
    for row in rows:
        reasons = list(row.unavailable)
        if row.is_anomalous:
            reasons.append('anomalous')
        if not row.is_completed:
            reasons.append('incomplete_set')
        if row.set_type not in {'normal', 'drop'}:
            reasons.append('non_working_set')
        if row.is_deload is True:
            reasons.append('deload_excluded_from_strength_change')
        result.extend(Exclusion(reason=r, exercise_id=row.exercise_id, workout_id=row.workout_id,
            set_id=row.set_id) for r in sorted(set(reasons)))
    return result


def _exercise(facts, query, exercise_id):
    rows = [r for r in facts.window_sets if r.exercise_id == exercise_id]
    prepared_intensity = prepare_intensity(facts.reference_sets)
    pure_points = session_points(rows)
    points = _points(facts, rows, prepared_intensity, pure_points)
    change = strength_change(pure_points) if facts.period else StrengthChange(None,None,None,None,None,())
    reason = ('unknown_block_end' if facts.period is None else
              'insufficient_sessions' if change.delta_pct is None else None)
    volume, intensity, effort = _volume(facts, rows), prepared_intensity.summary(rows), effort_summary(rows)
    valid = [p.e1rm for p in points if p.e1rm is not None]
    strength_coverage = Coverage(len(valid), len(points))
    record_rows = [r for r in facts.record_sets if r.exercise_id == exercise_id]
    records = record_history(record_rows)
    # Preview baseline cutoff and graph fold are independent. Full graph input
    # retains pre-window chronology, including observations after the cutoff.
    chronology = {r.set_id: r for r in facts.graph_sets if r.exercise_id == exercise_id}
    graph_rows = list(chronology.values())
    # Reuse only the exact same immutable facts; preview cutoffs still fold
    # their different graph chronology independently.
    same_rows = len(record_rows) == len(graph_rows) and all(
        chronology.get(row.set_id) is row for row in record_rows)
    events = (records if same_rows else record_history(graph_rows))['events']
    visible_ids = {r.set_id for r in rows}
    baseline = RecordBaseline(exercise_id=exercise_id, data_revision=facts.source_revision,
        gym_scope=_gym_scope(query.gym_id), as_of=facts.preview_start or facts.as_of or datetime.now(timezone.utc),
        excluded_workout_id=query.preview_workout_id, best_e1rm=records['best']['e1rm'] if records['best'] else None,
        best=records['best'], band=records['band'], weight_at_reps=records['weight_at_reps'], set_volume=records['set_volume'])
    excluded = _exclusions(rows)
    if reason:
        excluded.append(Exclusion(reason=reason, exercise_id=exercise_id))
    return ExerciseAnalytics(**_envelope(facts, query, exercise_id), points=points,
        summary=ExerciseSummary(best_e1rm=Metric(max(valid) if valid else None, None if valid else 'no_data', strength_coverage),
            strength=change, strength_reason=reason, volume=volume, intensity=intensity, effort=effort),
        record_baseline=baseline, record_events=[e for e in events if e['set_id'] in visible_ids],
        coverage=dict(strength=strength_coverage, volume=volume.tonnage.coverage,
            intensity=intensity.coverage, effort=effort.mean_rir.coverage), excluded=excluded)


def _muscles(facts, exercises, allowed_ids=None):
    primary = {m['exercise_id']:m['main_muscle_group'] for m in facts.catalog_mappings}
    result = muscle_progress({eid:ex.summary.strength for eid,ex in exercises.items()}, primary, allowed_ids=allowed_ids)
    enriched = []
    for muscle in result:
        parts = []
        for part in muscle.contributions:
            ex = exercises[part.exercise_id]
            parts.append(MuscleContribution(exercise_id=part.exercise_id,change=part.change,
                observations=tuple(ex.points),excluded=part.excluded,
                coverage=Coverage(len(ex.summary.strength.workout_ids),len(ex.points))))
        enriched.append(MuscleProgress(**{**asdict(muscle), 'contributions':tuple(parts)}))
    return enriched


async def exercise_analytics(db, user_id: int, exercise_id: int, query: AnalyticsQuery) -> ExerciseAnalytics:
    facts = await load_facts(db, user_id, query, exercise_ids={exercise_id}, allow_unresolved_period=True)
    # Catalog sidecar is owner-visible and independent of local history filters.
    if not any(m['exercise_id'] == exercise_id for m in facts.catalog_mappings):
        raise ValueError('exercise_not_found')
    return _exercise(facts, query, exercise_id)


async def muscle_analytics(db, user_id: int, query: AnalyticsQuery) -> MuscleAnalytics:
    facts = await load_facts(db, user_id, query, allow_unresolved_period=True)
    exercises = {eid:_exercise(facts,query,eid) for eid in sorted({r.exercise_id for r in facts.window_sets})}
    muscles = _muscles(facts, exercises)
    return MuscleAnalytics(**_envelope(facts,query), muscles=muscles, volume=_volume(facts,facts.window_sets),
        coverage=Coverage(sum(m.coverage.used for m in muscles),len(exercises)), excluded=_exclusions(facts.window_sets))


async def calendar_analytics(db, user_id: int, start, end):
    """Inclusive profile-local finish dates; no readiness or transaction ownership."""
    from api.schemas.analytics import CalendarAnalytics
    query = AnalyticsQuery(period='custom', start=start, end=end)
    facts = await load_facts(db, user_id, query)
    exercises = {eid: _exercise(facts, query, eid)
                 for eid in sorted({r.exercise_id for r in facts.window_sets})}
    workouts = [w for w in facts.global_workouts
                if facts.period.start_at <= w['finished_at'] < facts.period.end_exclusive]
    return CalendarAnalytics(**_envelope(facts, query), exercises=exercises,
        exercise_names={m['exercise_id']: m['name'] for m in facts.catalog_mappings},
        volume=_volume(facts, facts.window_sets),
        intensity=intensity_summary(facts.window_sets, facts.reference_sets),
        effort=effort_summary(facts.window_sets),
        adherence=AdherenceContext(**adherence_summary(facts.calendar, facts.global_workouts,
            today=facts.as_of.astimezone(ZoneInfo(facts.timezone)).date(),
            period=facts.period, is_closed=True)),
        sessions=len(workouts), total_minutes=sum(max(0, int(
            (w['finished_at'] - w['started_at']).total_seconds() // 60)) for w in workouts))


def _block(facts, query):
    block = next(b for b in facts.blocks if b['id'] == query.block_id)
    exercises = {eid:_exercise(facts,query,eid) for eid in sorted({r.exercise_id for r in facts.window_sets})}
    if facts.period:
        body = BodyContext(**body_changes(facts.measurements,facts.period))
        adherence = AdherenceContext(**adherence_summary(facts.calendar,facts.global_workouts,
            today=(facts.as_of or datetime.now(timezone.utc)).astimezone(ZoneInfo(facts.timezone)).date(),
            period=facts.period,is_closed=block['status']=='closed'))
    else:
        metrics = {}
        for key in sorted({r['metric_key'] for r in facts.measurements}):
            first = select_boundary_measurement(facts.measurements,key,block['start_date'],timezone=facts.timezone)
            if first:
                metrics[key] = dict(before=first,after=None,delta=None)
        body = BodyContext(metrics=metrics,reason='unknown_block_end')
        adherence = AdherenceContext(reason='unknown_block_end')
    return BlockAnalytics(**_envelope(facts,query),
        block=BlockMetadata(**{key:block.get(key) for key in BlockMetadata.model_fields}),
        exercises=exercises, muscles=_muscles(facts,exercises), volume=_volume(facts,facts.window_sets),
        intensity=intensity_summary(facts.window_sets,facts.reference_sets), effort=effort_summary(facts.window_sets),
        body=body,adherence=adherence, excluded=_exclusions(facts.window_sets))


async def block_analytics(db, user_id: int, block_id: int, gym_id: str | None) -> BlockAnalytics:
    query = AnalyticsQuery(period='block',block_id=block_id,gym_id=gym_id)
    return _block(await load_facts(db,user_id,query,allow_unresolved_period=True),query)


def _difference(a, b, unit, *, scope='local_load', reason='unavailable_metric'):
    return Difference(value=b-a if a is not None and b is not None else None, unit=unit,
        scope=scope, reason=None if a is not None and b is not None else reason)


def _adherence_ratio(context):
    return 100*context.completed_days/context.planned_days if context.planned_days and context.reason is None else None


async def compare_blocks(db, user_id: int, block_a_id: int, block_b_id: int, gym_id: str | None) -> BlockComparison:
    qa = AnalyticsQuery(period='block',block_id=block_a_id,gym_id=gym_id)
    qb = AnalyticsQuery(period='block',block_id=block_b_id,gym_id=gym_id)
    fa = await load_facts(db,user_id,qa,allow_unresolved_period=True)
    fb = await load_facts(db,user_id,qb,allow_unresolved_period=True)
    a,b = _block(fa,qa),_block(fb,qb)
    if a.block.status != 'closed' or b.block.status != 'closed':
        raise ValueError('comparison_requires_closed_blocks')
    ids_a,ids_b = set(a.exercises),set(b.exercises)
    common = {eid for eid in ids_a & ids_b if a.exercises[eid].summary.strength.delta_pct is not None
              and b.exercises[eid].summary.strength.delta_pct is not None}
    a.muscles,b.muscles = _muscles(fa,a.exercises,common),_muscles(fb,b.exercises,common)
    ma,mb = {m.muscle_key:m.delta_pct for m in a.muscles},{m.muscle_key:m.delta_pct for m in b.muscles}
    differences = ComparisonDifferences(
        muscles={k:_difference(ma.get(k),mb.get(k),'percentage_points',reason='no_common_exercises') for k in sorted(ma.keys()|mb.keys())},
        work_sets_per_7_days=_difference(a.volume.work_sets_per_7_days,b.volume.work_sets_per_7_days,'sets_per_7_days'),
        drop_sets_per_7_days=_difference(a.volume.drop_sets_per_7_days,b.volume.drop_sets_per_7_days,'sets_per_7_days'),
        tonnage_per_7_days=_difference(a.volume.tonnage_per_7_days.value,b.volume.tonnage_per_7_days.value,'kg_per_7_days'),
        intensity=_difference(a.intensity.value,b.intensity.value,'percentage_points'),
        mean_rir=_difference(a.effort.mean_rir.value,b.effort.mean_rir.value,'repetitions'),
        drift_rir=_difference(a.effort.drift_rir.value,b.effort.drift_rir.value,'repetitions'),
        adherence=_difference(_adherence_ratio(a.adherence),_adherence_ratio(b.adherence),'percentage_points',scope='block_context'),
        body={k:_difference(a.body.metrics[k].delta if k in a.body.metrics else None,
            b.body.metrics[k].delta if k in b.body.metrics else None,
            'kg' if k=='weight' else 'percentage_points' if k=='body_fat' else 'cm',scope='block_context')
            for k in sorted(a.body.metrics.keys()|b.body.metrics.keys())})
    excluded = [Exclusion(exercise_id=eid,reason='insufficient_sessions') for eid in sorted((ids_a & ids_b)-common)]
    if not common:
        excluded.append(Exclusion(reason='no_common_exercises'))
    for side in (a,b):
        if side.scope.reason:
            excluded.append(Exclusion(reason=side.scope.reason))
    envelope = _envelope(fa,qa)
    envelope['scope'] = AnalyticsScope(period=None,timezone=fa.timezone,gym_id=gym_id,
        gym_scope=_gym_scope(gym_id),block_ids=[block_a_id,block_b_id])
    # Task13 must validate both source revisions against its read fences.
    envelope['data_revision'] = fa.source_revision
    description = '; '.join(f'{key}: A {ma.get(key)}%, B {mb.get(key)}%, B-A {value.value} percentage points'
        for key,value in differences.muscles.items() if value.value is not None) or 'No common exercises with sufficient history.'
    return BlockComparison(**envelope,a=a,b=b,common_exercise_ids=sorted(common),added=sorted(ids_b-ids_a),
        removed=sorted(ids_a-ids_b),excluded=excluded,differences=differences,description=description)
