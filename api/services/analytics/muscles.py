"""Primary-muscle strength changes with one vote per stable exercise ID."""
from collections import defaultdict
from statistics import mean
from .types import Coverage, MuscleExerciseContribution, MuscleProgress, StrengthChange


def muscle_progress(changes: dict[int, StrengthChange], primary_by_exercise: dict[int, str],
                    *, allowed_ids: set[int] | None = None) -> list[MuscleProgress]:
    groups = defaultdict(list)
    for exercise_id, change in sorted(changes.items()):
        key = primary_by_exercise.get(exercise_id)
        if not key:
            continue
        reasons = ('not_common',) if allowed_ids is not None and exercise_id not in allowed_ids else ()
        if change.delta_pct is None:
            reasons += ('insufficient_sessions',)
        groups[key].append(MuscleExerciseContribution(exercise_id, change, excluded=reasons))
    result = []
    for key, parts in sorted(groups.items()):
        used = [p for p in parts if not p.excluded]
        reasons = tuple(sorted({r for p in parts for r in p.excluded}))
        if not used and allowed_ids is not None:
            reasons += ('no_common_exercises',)
        result.append(MuscleProgress(key, mean(p.change.delta_pct for p in used) if used else None,
            tuple(p.exercise_id for p in used), reasons, tuple(parts), Coverage(len(used), len(parts))))
    return result
