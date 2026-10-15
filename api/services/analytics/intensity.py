"""Pure historical intensity; reference input is already in the chosen gym scope."""
from collections import defaultdict
from datetime import timedelta
from .policy import positive_reps, strictly_greater
from .strength import _admitted, _e1rm, _order
from .types import Coverage, Metric, NormalizedSetFact


def intensity_summary(window: list[NormalizedSetFact],
                      reference: list[NormalizedSetFact]) -> Metric:
    """Weight admitted set percentages by actual repetitions, using prior e1RM.

    The repository supplies a union of historical candidates. Reapply the
    exact boundary for each target, so neighboring visible sessions cannot
    provide a current or future reference to an earlier session.
    """
    history = defaultdict(list)
    for row in sorted((r for r in reference if _admitted(r)
                       and r.is_deload is not True), key=_order):
        value = _e1rm(row)
        if value is not None:
            history[row.exercise_id].append((row, value))
    baselines = {}
    weighted, reps, used, total = 0., 0, 0, 0
    for row in window:
        if not _admitted(row):
            continue
        total += 1
        if _e1rm(row) is None or not positive_reps(row.reps):
            continue
        key = row.workout_id, row.exercise_id, row.started_at
        if key not in baselines:
            best = None
            for candidate, value in history[row.exercise_id]:
                if (candidate.workout_id != row.workout_id
                        and row.started_at - timedelta(days=56) <= candidate.finished_at < row.started_at
                        and (best is None or strictly_greater(value, best))):
                    best = value
            baselines[key] = best
        baseline = baselines[key]
        if baseline is None:
            continue
        weighted += row.e1rm_weight_kg / baseline * 100 * row.reps
        reps += row.reps
        used += 1
    return Metric(weighted / reps if reps else None,
        None if reps else 'insufficient_intensity_reference', Coverage(used, total))
