"""Unrounded set-weighted effort and comparable exercise/workout drift."""
from collections import defaultdict
from statistics import mean, median

from .policy import eligible, finite_number, positive_reps
from .types import Coverage, EffortSummary, Metric, NormalizedSetFact


def effort_summary(rows: list[NormalizedSetFact]) -> EffortSummary:
    normal = [row for row in rows if eligible(row, 'work_sets')]
    ratings = []
    pairs = defaultdict(lambda: defaultdict(list))
    for row in normal:
        groups = pairs[row.exercise_id, row.workout_id]
        rir = finite_number(row.rir)
        if rir is None or rir < 0:
            continue
        ratings.append(rir)
        # RIR and counts survive missing e1RM repetitions. Drift still needs
        # known, identical performed load/repetitions within each group.
        weight = finite_number(row.e1rm_weight_kg)
        recorded = finite_number(row.weight_kg)
        if (weight is None and recorded is None) or not positive_reps(row.reps):
            continue
        context = row.load_context or {}
        key = (row.comparison_key, row.setup_id, weight, recorded, row.reps,
               context.get('left_reps'), context.get('right_reps'))
        groups[key].append(row)

    pair_drifts = []
    for groups in pairs.values():
        drifts = []
        for group in groups.values():
            group.sort(key=lambda row: (row.exercise_order, row.set_number, row.set_id))
            if len(group) >= 2:
                drifts.append(group[-1].rir - group[0].rir)
        if drifts:
            pair_drifts.append(median(drifts))
    return EffortSummary(
        mean_rir=Metric(mean(ratings) if ratings else None,
            None if ratings else 'missing_rir', Coverage(len(ratings), len(normal))),
        drift_rir=Metric(median(pair_drifts) if pair_drifts else None,
            None if pair_drifts else 'insufficient_comparable_rir',
            Coverage(len(pair_drifts), len(pairs))),
    )
