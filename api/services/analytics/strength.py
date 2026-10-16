"""Pure strength observations and full-history record chronology.

Callers supply the selected gym scope; record_history and strength_change
operate on one exercise. Date filtering belongs to the caller, after folding
the entire scoped history for PRs, never before it.
"""
from collections import defaultdict
from statistics import mean, median
from .load import lombardi
from .policy import finite_number, strictly_greater
from .types import (Coverage, ExerciseSessionPoint, NormalizedSetFact,
                    PointDetails, StrengthChange)


def _admitted(row: NormalizedSetFact, *, drops: bool = False) -> bool:
    return (row.is_completed and row.finished_at is not None
            and not row.is_anomalous
            and row.set_type in ({'normal', 'drop'} if drops else {'normal'}))


def _order(row: NormalizedSetFact) -> tuple:
    return row.finished_at, row.workout_id, row.set_number, row.set_id


def _e1rm(row: NormalizedSetFact) -> float | None:
    # Derived only from immutable scalar fields. dataclasses.replace creates a
    # fresh fact without this private memo; no cross-request/global cache.
    if '_prepared_e1rm' not in row.__dict__:
        object.__setattr__(row, '_prepared_e1rm', lombardi(row.e1rm_weight_kg, row.e1rm_reps))
    return row.__dict__['_prepared_e1rm']


def session_points(rows: list[NormalizedSetFact]) -> list[ExerciseSessionPoint]:
    """Aggregate every instance of an exercise into one workout observation."""
    groups = defaultdict(list)
    for row in rows:
        if _admitted(row, drops=True):
            groups[row.workout_id, row.exercise_id].append(row)
    points = []
    for (workout_id, exercise_id), group in groups.items():
        group.sort(key=_order)
        normal = [row for row in group if row.set_type == 'normal']
        best, best_value = None, None
        used = 0
        for row in normal:
            value = _e1rm(row)
            if value is not None:
                used += 1
                if best is None or (value > best_value and strictly_greater(value, best_value)):
                    best, best_value = row, value
        source = best or group[0]
        volumes = [row.tonnage_kg for row in group if finite_number(row.tonnage_kg) is not None]
        points.append(ExerciseSessionPoint(workout_id=workout_id, exercise_id=exercise_id,
            at=source.finished_at, e1rm=best_value,
            volume=sum(volumes) if volumes else None, work_sets=len(normal),
            drop_sets=len(group)-len(normal), is_deload=source.is_deload,
            gym_id=source.gym_id, gym_label=source.gym_label,
            source_set_id=best.set_id if best else None,
            details=PointDetails(load_context=source.load_context,
                session_context=source.session_context, coverage=Coverage(used, len(normal)),
                reasons=tuple(sorted({reason for row in group for reason in row.unavailable})),
                observation_dates=(source.finished_at.date(),))))
    return sorted(points, key=lambda point: (point.at, point.workout_id, point.exercise_id))


def strength_change(points: list[ExerciseSessionPoint]) -> StrengthChange:
    usable = sorted((point for point in points if point.is_deload is not True
        and finite_number(point.e1rm) is not None and point.e1rm > 0),
        key=lambda point: (point.at, point.workout_id))
    if len(usable) < 4:
        return StrengthChange(None, None, None, None, None, ())
    size, aggregate = (3, median) if len(usable) >= 6 else (2, mean)
    first, last = usable[:size], usable[-size:]
    start, end = aggregate(p.e1rm for p in first), aggregate(p.e1rm for p in last)
    delta = end - start
    return StrengthChange(start, end, delta, delta / start * 100,
        f'{size}+{size}', tuple(p.workout_id for p in first + last))


def _band(reps: int) -> str:
    for high, name in ((3, '1-3'), (6, '4-6'), (10, '7-10'), (15, '11-15')):
        if reps <= high:
            return name
    return '16+'


def _record_entry(row):
    return dict(exercise_id=row.exercise_id, workout_id=row.workout_id,
        exercise_instance_id=row.exercise_instance_id, set_id=row.set_id,
        set_number=row.set_number, at=row.finished_at, weight=row.e1rm_weight_kg,
        reps=row.e1rm_reps, actual_reps=row.reps, gym_id=row.gym_id,
        gym_label=row.gym_label, is_deload=row.is_deload,
        load_context=row.load_context, session_context=row.session_context)


def _improves(value, previous):
    return previous is None or (value > previous and strictly_greater(value, previous))


def record_history(rows: list[NormalizedSetFact]) -> dict:
    """Fold full single-exercise history, retaining initial and strict PR events.

    ``best`` is the historical baseline; ``events`` excludes the initial result.
    Each entry retains its source and raw normalized load with no display rounding.
    Public baseline versions/as-of and visible event slicing belong to DTO assembly.
    """
    initial = best = volume = None
    events, bands, weight_at_reps = [], {}, {}
    for row in sorted((row for row in rows if _admitted(row)), key=_order):
        value = _e1rm(row)
        wins_best = wins_band = wins_weight = False
        if value is not None:
            band = _band(row.e1rm_reps)
            key = str(row.e1rm_reps)
            wins_best = _improves(value, best['e1rm'] if best else None)
            wins_band = _improves(value, bands[band]['e1rm'] if band in bands else None)
            wins_weight = row.e1rm_reps <= 12 and _improves(row.e1rm_weight_kg,
                weight_at_reps[key]['weight'] if key in weight_at_reps else None)
        wins_volume = finite_number(row.tonnage_kg) is not None and _improves(
            row.tonnage_kg, volume['value'] if volume else None)
        if not (wins_best or wins_band or wins_weight or wins_volume):
            continue
        entry = _record_entry(row)
        if wins_best or wins_band:
            result = {**entry, 'e1rm': value}
        if wins_best:
            if best is None:
                initial = best = result
            else:
                events.append({**result, 'previous_e1rm': best['e1rm']})
                best = result
        if wins_band:
            bands[band] = result
        if wins_weight:
            weight_at_reps[key] = entry
        if wins_volume:
            volume = {**entry, 'value': row.tonnage_kg}
    return dict(initial=initial, best=best, events=events, band=bands,
                weight_at_reps=weight_at_reps, set_volume=volume)
