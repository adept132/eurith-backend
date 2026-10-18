from api.routers.workout_history import _translate_history_error
from api.services.workout_history import (
    WorkoutHistoryIdempotencyConflict,
    WorkoutHistoryRevisionConflict,
    draft_from_workout,
)
from tests.test_workout_history_service import _legacy_workout


def test_revision_conflict_returns_structured_legacy_empty_workout_snapshot():
    snapshot = draft_from_workout(_legacy_workout(with_empty_exercise=False))

    response = _translate_history_error(WorkoutHistoryRevisionConflict(2, snapshot))

    assert response.status_code == 409
    assert response.detail == {
        "error": "history_revision_conflict",
        "server_revision": 2,
        "server_draft": snapshot.model_dump(mode="json"),
    }
    assert response.detail["server_draft"]["exercises"] == []


def test_idempotency_conflict_returns_structured_legacy_empty_sets_snapshot():
    snapshot = draft_from_workout(_legacy_workout(with_empty_exercise=True))

    response = _translate_history_error(WorkoutHistoryIdempotencyConflict(3, snapshot))

    assert response.status_code == 409
    assert response.detail["error"] == "history_idempotency_conflict"
    assert response.detail["server_workout_id"] == 3
    assert response.detail["server_revision"] == 2
    assert response.detail["server_draft"]["exercises"][0]["sets"] == []
