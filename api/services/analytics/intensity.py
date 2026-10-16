"""Pure historical intensity; reference input is already in the chosen gym scope."""
from collections import defaultdict
from datetime import timedelta
from .policy import positive_reps, strictly_greater
from .strength import _admitted, _e1rm, _order
from .types import Coverage, Metric, NormalizedSetFact


class PreparedIntensity:
    """Request-local immutable history plus memoized workout reference values.

    Every target still applies the original chronological boundary and tolerance.
    Reuse only within the same loaded facts/gym scope; never across requests.
    """
    def __init__(self, reference):
        self.history = defaultdict(list)
        self.baselines = {}
        seen = set()
        for row in sorted((r for r in reference if _admitted(r)
                           and r.is_deload is not True), key=_order):
            value = _e1rm(row)
            if value is not None:
                # Identical candidates have identical boundary/owner exclusion
                # behavior and cannot change the monotonic tolerance fold twice.
                key = row.exercise_id, row.workout_id, row.finished_at, value
                if key in seen:
                    continue
                seen.add(key)
                self.history[row.exercise_id].append((row, value))

    def summary(self, window):
        weighted, reps, used, total = 0., 0, 0, 0
        for row in window:
            if not _admitted(row):
                continue
            total += 1
            if _e1rm(row) is None or not positive_reps(row.reps):
                continue
            key = row.workout_id, row.exercise_id, row.started_at
            if key not in self.baselines:
                best = None
                lower = row.started_at - timedelta(days=56)
                for candidate, value in self.history[row.exercise_id]:
                    if (candidate.workout_id != row.workout_id
                            and lower <= candidate.finished_at < row.started_at
                            and (best is None or (value > best and strictly_greater(value, best)))):
                        best = value
                self.baselines[key] = best
            baseline = self.baselines[key]
            if baseline is None:
                continue
            weighted += row.e1rm_weight_kg / baseline * 100 * row.reps
            reps += row.reps
            used += 1
        return Metric(weighted / reps if reps else None,
            None if reps else 'insufficient_intensity_reference', Coverage(used, total))


def prepare_intensity(reference: list[NormalizedSetFact]) -> PreparedIntensity:
    return PreparedIntensity(reference)


def intensity_summary(window: list[NormalizedSetFact],
                      reference: list[NormalizedSetFact]) -> Metric:
    """Weight admitted percentages by repetitions with a scoped prior reference."""
    return prepare_intensity(reference).summary(window)
