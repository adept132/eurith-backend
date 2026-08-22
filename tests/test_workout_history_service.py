from api.schemas.workout_history import WorkoutHistorySetDraft
from api.services.models import WorkoutSessionExercise, WorkoutSessionSet
from api.services.workout_history import (
    _apply_set_parent_links,
    _draft_sets_from_exercise,
)


def _set(*, set_id: int, client_uuid: str, set_number: int) -> WorkoutSessionSet:
    return WorkoutSessionSet(
        id=set_id,
        workout_session_exercise_id=7,
        client_uuid=client_uuid,
        set_number=set_number,
        set_type="normal",
        weight=80,
        reps=8,
        effort_level="medium",
        notes=None,
        is_completed=True,
    )


def _draft_set(
    *, client_uuid: str, set_number: int, parent_client_uuid: str | None = None
) -> WorkoutHistorySetDraft:
    return WorkoutHistorySetDraft(
        client_uuid=client_uuid,
        parent_client_uuid=parent_client_uuid,
        set_number=set_number,
        set_type="drop" if parent_client_uuid else "normal",
        weight=80,
        reps=8,
        effort_level="medium",
        notes=None,
        is_completed=True,
    )


def test_apply_set_parent_links_resolves_client_uuid_to_persistence_id():
    root = _set(set_id=101, client_uuid="root", set_number=1)
    drop = _set(set_id=102, client_uuid="drop", set_number=2)

    _apply_set_parent_links(
        [
            _draft_set(client_uuid="root", set_number=1),
            _draft_set(
                client_uuid="drop",
                set_number=2,
                parent_client_uuid="root",
            ),
        ],
        {"root": root, "drop": drop},
    )

    assert root.parent_set_id is None
    assert drop.parent_set_id == root.id


def test_draft_sets_round_trip_existing_parent_as_client_uuid():
    root = _set(set_id=101, client_uuid="root", set_number=1)
    drop = _set(set_id=102, client_uuid="drop", set_number=2)
    drop.set_type = "drop"
    drop.parent_set_id = root.id
    exercise = WorkoutSessionExercise(
        id=7,
        workout_session_id=3,
        exercise_id=9,
        order_index=0,
        sets=[root, drop],
    )

    result = _draft_sets_from_exercise(exercise)

    assert result[0]["parent_client_uuid"] is None
    assert result[1]["parent_client_uuid"] == "root"
