from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from api.services.workout_structure import extract_workout_structure


def _finished_workout():
    prescription = {
        "scheme": "double_progression",
        "sets": [
            {
                "set_number": number,
                "weight_kg": 82.5,
                "rep_min": 8,
                "rep_max": 10,
                "rir": 2,
                "kind": "normal",
            }
            for number in (1, 2, 3)
        ],
    }
    sets = [
        SimpleNamespace(
            id=700 + number,
            set_number=number,
            set_type="normal",
            weight=82.5,
            reps=8 if number == 3 else 10,
            effort_level="medium",
            notes=None,
            is_completed=True,
        )
        for number in (1, 2, 3)
    ]
    exercise = SimpleNamespace(
        id=70,
        exercise_id=42,
        order_index=0,
        superset_group=None,
        notes="Фактическая заметка упражнения",
        target_sets=3,
        prescription=prescription,
        live_prescription=prescription,
        recommended_weight=82.5,
        sets=sets,
    )
    return SimpleNamespace(
        id=7,
        notes="Фактическая заметка сессии",
        started_at=datetime(2026, 8, 22, 9, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 22, 10, tzinfo=timezone.utc),
        exercises=[exercise],
    )


def test_extract_workout_structure_matches_mobile_golden_without_facts():
    structure = extract_workout_structure(_finished_workout())

    assert structure.model_dump(mode="json") == {
        "name": "Тренировка 22 августа",
        "notes": None,
        "exercises": [
            {
                "exercise_id": 42,
                "order_index": 0,
                "superset_group": None,
                "target_sets": 3,
                "set_kinds": ["normal", "normal", "normal"],
                "rep_min": 8,
                "rep_max": 10,
                "target_rir": 2,
                "rest_seconds": None,
                "notes": None,
            }
        ],
    }
    serialized = json.dumps(structure.model_dump(mode="json"))
    assert "82.5" not in serialized
    assert "finished_at" not in serialized
    assert "is_completed" not in serialized


def test_extract_falls_back_to_completed_work_sets_and_explicit_notes():
    workout = _finished_workout()
    workout.exercises[0].target_sets = None
    workout.exercises[0].prescription = None
    workout.exercises[0].live_prescription = None
    workout.exercises[0].sets[2].set_type = "drop"

    structure = extract_workout_structure(
        workout,
        persistent_note_ids={"workout:7", "exercise:70"},
    )

    assert structure.notes == "Фактическая заметка сессии"
    assert structure.exercises[0].target_sets == 2
    assert structure.exercises[0].set_kinds == ["normal", "normal"]
    assert structure.exercises[0].rep_min == 10
    assert structure.exercises[0].rep_max == 10
    assert structure.exercises[0].notes == "Фактическая заметка упражнения"
