from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from api.schemas.workout_history import WorkoutHistoryDraft
from api.services.models import WorkoutRecalculationOperation, WorkoutSession


T0 = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


def _set(set_number: int, *, client_uuid: str | None = None) -> dict:
    return {
        "client_uuid": client_uuid or f"set-{set_number}",
        "set_number": set_number,
        "set_type": "normal",
        "weight": "82.50",
        "reps": 8,
        "effort_level": "medium",
        "notes": None,
        "is_completed": True,
    }


def _draft(**overrides) -> dict:
    payload = {
        "client_uuid": "history-schema-workout",
        "base_revision": 0,
        "entry_mode": "manual",
        "started_at": T0,
        "finished_at": T1,
        "notes": None,
        "session_rpe": 7.5,
        "exercises": [
            {
                "client_uuid": "exercise-1",
                "exercise_id": 101,
                "order_index": 0,
                "superset_group": None,
                "notes": None,
                "sets": [_set(1)],
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_history_draft_rejects_negative_duration():
    with pytest.raises(ValidationError):
        WorkoutHistoryDraft(**_draft(started_at=T1, finished_at=T0))


@pytest.mark.parametrize(
    "exercises",
    [
        [],
        [
            {
                "client_uuid": "exercise-without-sets",
                "exercise_id": 101,
                "order_index": 0,
                "superset_group": None,
                "notes": None,
                "sets": [],
            }
        ],
    ],
    ids=["no-exercises", "exercise-without-sets"],
)
def test_history_draft_rejects_empty_training_content(exercises):
    with pytest.raises(ValidationError):
        WorkoutHistoryDraft.model_validate(_draft(exercises=exercises))


def test_history_draft_requires_exercises_field():
    payload = _draft()
    payload.pop("exercises")

    with pytest.raises(ValidationError):
        WorkoutHistoryDraft.model_validate(payload)


def test_history_draft_requires_sets_field():
    payload = _draft()
    payload["exercises"][0].pop("sets")

    with pytest.raises(ValidationError):
        WorkoutHistoryDraft.model_validate(payload)


def test_history_draft_rejects_duplicate_set_numbers():
    payload = _draft()
    payload["exercises"][0]["sets"] = [
        _set(1, client_uuid="set-a"),
        _set(1, client_uuid="set-b"),
    ]

    with pytest.raises(ValidationError):
        WorkoutHistoryDraft(**payload)


def test_history_draft_rejects_immutable_prescription_fields():
    payload = _draft()
    payload["exercises"][0]["prescription"] = {"target_reps": 10}

    with pytest.raises(ValidationError):
        WorkoutHistoryDraft(**payload)


def test_history_draft_normalizes_blank_notes():
    payload = _draft(notes="   ")
    payload["exercises"][0]["notes"] = "\n"
    payload["exercises"][0]["sets"][0]["notes"] = " "

    draft = WorkoutHistoryDraft(**payload)

    assert draft.notes is None
    assert draft.exercises[0].notes is None
    assert draft.exercises[0].sets[0].notes is None


def test_history_draft_accepts_dropset_parent_client_uuid():
    payload = _draft()
    payload["exercises"][0]["sets"] = [
        _set(1, client_uuid="root-set"),
        {
            **_set(2, client_uuid="drop-set"),
            "set_type": "drop",
            "parent_client_uuid": "root-set",
        },
    ]

    draft = WorkoutHistoryDraft(**payload)

    assert draft.exercises[0].sets[1].parent_client_uuid == "root-set"


def test_history_draft_rejects_dangling_parent_client_uuid_with_context():
    payload = _draft()
    payload["exercises"][0]["sets"] = [
        {
            **_set(1, client_uuid="drop-set"),
            "set_type": "drop",
            "parent_client_uuid": "missing-root",
        }
    ]

    with pytest.raises(ValidationError) as error:
        WorkoutHistoryDraft(**payload)

    assert "parent_client_uuid 'missing-root' must reference a set in the same exercise" in str(
        error.value
    )


def test_history_metadata_models_expose_revision_and_durable_operation_key():
    workout_columns = WorkoutSession.__table__.columns
    operation_constraints = WorkoutRecalculationOperation.__table__.constraints

    assert {"entry_mode", "revision", "edited_at"} <= set(workout_columns.keys())
    assert any(
        set(constraint.columns.keys()) == {"workout_id", "revision"}
        for constraint in operation_constraints
        if hasattr(constraint, "columns")
    )
