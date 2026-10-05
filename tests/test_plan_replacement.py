from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from api.services.plan_replacement import build_replacement_preview
from api.services.workout_structure import (
    WorkoutStructureDraft,
    WorkoutStructureExerciseDraft,
)


def _source_structure() -> WorkoutStructureDraft:
    return WorkoutStructureDraft(
        name="Push replacement",
        notes=None,
        exercises=[
            WorkoutStructureExerciseDraft(
                exercise_id=1,
                order_index=0,
                target_sets=4,
                set_kinds=["normal"] * 4,
                rep_min=8,
                rep_max=10,
                target_rir=2,
                rest_seconds=120,
            ),
            WorkoutStructureExerciseDraft(
                exercise_id=3,
                order_index=1,
                target_sets=4,
                set_kinds=["normal"] * 4,
                rep_min=10,
                rep_max=12,
                target_rir=3,
                rest_seconds=90,
            ),
        ],
    )


def _target_plan(revision: int = 3):
    return SimpleNamespace(
        id=10,
        revision=revision,
        exercises=[
            SimpleNamespace(
                exercise_id=1,
                order_index=0,
                superset_group_id=None,
                target_sets=3,
                override_reps="6-8",
                override_rir=3,
            ),
            SimpleNamespace(
                exercise_id=2,
                order_index=1,
                superset_group_id=None,
                target_sets=4,
                override_reps="10",
                override_rir=2,
            ),
        ],
    )


def _affected_days():
    return [
        SimpleNamespace(id=102, target_date=date(2026, 8, 29)),
        SimpleNamespace(id=101, target_date=date(2026, 8, 22)),
    ]


def test_preview_has_exact_diff_counts_dates_and_stable_token():
    preview = build_replacement_preview(
        source_kind="routine",
        source_id=55,
        source_structure=_source_structure(),
        target_plan=_target_plan(),
        affected_days=_affected_days(),
        exercise_names={1: "Bench press", 2: "Incline press", 3: "Dips"},
    )

    assert preview.effective_from == date(2026, 8, 22)
    assert preview.affected_dates == [date(2026, 8, 22), date(2026, 8, 29)]
    assert preview.old_total_sets == 7
    assert preview.new_total_sets == 8
    assert [(item.exercise_id, item.old_target_sets, item.new_target_sets) for item in preview.added] == [
        (3, None, 4)
    ]
    assert [(item.exercise_id, item.old_target_sets, item.new_target_sets) for item in preview.removed] == [
        (2, 4, None)
    ]
    assert [(item.exercise_id, item.old_target_sets, item.new_target_sets) for item in preview.modified] == [
        (1, 3, 4)
    ]
    assert preview.preview_token == "1cab8d006f11cc6ddc4b5ac562f58c4843d145e428aaaa9f3e6bbd92195b0894"


def test_preview_token_changes_with_plan_revision_affected_ids_or_structure():
    common = dict(
        source_kind="routine",
        source_id=55,
        source_structure=_source_structure(),
        target_plan=_target_plan(),
        affected_days=_affected_days(),
        exercise_names={1: "Bench press", 2: "Incline press", 3: "Dips"},
    )
    original = build_replacement_preview(**common).preview_token

    revised = build_replacement_preview(
        **{**common, "target_plan": _target_plan(revision=4)}
    ).preview_token
    changed_target = _target_plan()
    changed_target.exercises[0].target_sets = 9
    target_structure_changed = build_replacement_preview(
        **{**common, "target_plan": changed_target}
    ).preview_token
    fewer_days = build_replacement_preview(
        **{**common, "affected_days": _affected_days()[:1]}
    ).preview_token
    changed_structure = _source_structure()
    changed_structure.exercises[0].target_sets = 5
    changed_structure.exercises[0].set_kinds.append("normal")
    changed = build_replacement_preview(
        **{**common, "source_structure": changed_structure}
    ).preview_token

    assert len({original, revised, target_structure_changed, fewer_days, changed}) == 5
