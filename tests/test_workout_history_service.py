from datetime import datetime, timedelta, timezone

import pytest

import api.services.workout_history as workout_history_service
from api.schemas.workout_history import WorkoutHistoryDraft, WorkoutHistorySetDraft
from api.services.models import WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet
from api.services.workout_history import (
    WorkoutHistoryIdempotencyConflict,
    WorkoutHistoryRevisionConflict,
    _require_exact_history_replay,
    _history_request_fingerprint,
    _apply_set_parent_links,
    _draft_sets_from_exercise,
    draft_from_workout,
    create_history_workout,
    replace_history_workout,
)


T0 = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)


def _legacy_workout(*, with_empty_exercise: bool) -> WorkoutSession:
    exercises = []
    if with_empty_exercise:
        exercises.append(
            WorkoutSessionExercise(
                id=7,
                workout_session_id=3,
                client_uuid="legacy-exercise",
                exercise_id=9,
                order_index=0,
                sets=[],
            )
        )
    return WorkoutSession(
        id=3,
        app_user_id=1,
        client_uuid="legacy-workout",
        source="free",
        status="finished",
        entry_mode="manual",
        revision=2,
        started_at=T0,
        finished_at=T1,
        exercises=exercises,
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


def _matching_correction() -> tuple[WorkoutSession, WorkoutHistoryDraft]:
    exercise = WorkoutSessionExercise(
        id=7,
        workout_session_id=3,
        client_uuid="request-exercise",
        exercise_id=9,
        order_index=0,
        superset_group=None,
        notes="exercise note",
        sets=[_set(set_id=101, client_uuid="request-set", set_number=1)],
    )
    exercise.exercise = workout_history_service.Exercise(id=9, name="Жим лёжа")
    workout = WorkoutSession(
        id=3,
        app_user_id=1,
        client_uuid="persisted-workout",
        source="free",
        status="finished",
        entry_mode="manual",
        revision=2,
        started_at=T0,
        finished_at=T1,
        notes="server note",
        session_rpe=7,
        exercises=[exercise],
    )
    request = WorkoutHistoryDraft(
        client_uuid="correction-request",
        base_revision=1,
        entry_mode="manual",
        started_at=T0,
        finished_at=T1,
        notes="server note",
        session_rpe=7,
        exercises=[
            {
                "client_uuid": "request-exercise",
                "exercise_id": 9,
                "order_index": 0,
                "superset_group": None,
                "notes": "exercise note",
                "sets": [
                    {
                        "client_uuid": "request-set",
                        "set_number": 1,
                        "set_type": "normal",
                        "weight": 80,
                        "reps": 8,
                        "effort_level": "medium",
                        "notes": None,
                        "is_completed": True,
                    }
                ],
            }
        ],
    )
    return workout, request


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


@pytest.mark.parametrize("with_empty_exercise", [False, True])
def test_draft_from_workout_preserves_legacy_empty_content(with_empty_exercise):
    snapshot = draft_from_workout(
        _legacy_workout(with_empty_exercise=with_empty_exercise)
    )

    assert len(snapshot.exercises) == int(with_empty_exercise)
    if with_empty_exercise:
        assert snapshot.exercises[0].sets == []


def test_idempotency_conflict_carries_legacy_empty_snapshot():
    workout = _legacy_workout(with_empty_exercise=True)
    workout.history_request_fingerprint = "stored-fingerprint"

    with pytest.raises(WorkoutHistoryIdempotencyConflict) as error:
        _require_exact_history_replay(workout, "different-fingerprint")

    assert error.value.server_draft.exercises[0].sets == []


@pytest.mark.asyncio
async def test_revision_conflict_carries_legacy_empty_snapshot(monkeypatch):
    workout = _legacy_workout(with_empty_exercise=False)

    async def load_legacy_workout(*args, **kwargs):
        return workout

    class FakeDb:
        rolled_back = False

        async def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(
        workout_history_service,
        "_load_owned_workout",
        load_legacy_workout,
    )
    db = FakeDb()
    request = WorkoutHistoryDraft(
        client_uuid="correction-request",
        base_revision=1,
        entry_mode="manual",
        started_at=T0,
        finished_at=T1,
        exercises=[
            {
                "client_uuid": "request-exercise",
                "exercise_id": 9,
                "order_index": 0,
                "sets": [
                    {
                        "client_uuid": "request-set",
                        "set_number": 1,
                        "reps": 8,
                    }
                ],
            }
        ],
    )

    with pytest.raises(WorkoutHistoryRevisionConflict) as error:
        await replace_history_workout(db, 1, workout.id, request)

    assert error.value.server_revision == 2
    assert error.value.server_draft.exercises == []
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_exact_correction_replay_after_lost_response_returns_current_workout(
    monkeypatch,
):
    workout, request = _matching_correction()

    async def load_workout(*args, **kwargs):
        return workout

    class FakeDb:
        rolled_back = False

        async def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(workout_history_service, "_load_owned_workout", load_workout)
    db = FakeDb()

    result = await replace_history_workout(db, 1, workout.id, request)

    assert result is workout
    assert result.revision == request.base_revision + 1
    assert db.rolled_back is False


@pytest.mark.asyncio
async def test_different_correction_at_same_stale_revision_remains_a_conflict(monkeypatch):
    workout, request = _matching_correction()
    request = request.model_copy(update={"notes": "different concurrent edit"})

    async def load_workout(*args, **kwargs):
        return workout

    class FakeDb:
        rolled_back = False

        async def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(workout_history_service, "_load_owned_workout", load_workout)
    db = FakeDb()

    with pytest.raises(WorkoutHistoryRevisionConflict) as error:
        await replace_history_workout(db, 1, workout.id, request)

    assert error.value.server_revision == 2
    assert error.value.server_draft.exercises[0].exercise_name == "Жим лёжа"
    assert db.rolled_back is True


@pytest.mark.asyncio
async def test_create_integrity_race_reloads_exact_winner_deterministically(monkeypatch):
    workout, correction = _matching_correction()
    request = correction.model_copy(
        update={"base_revision": 0, "client_uuid": "racing-create"}
    )
    workout.client_uuid = request.client_uuid
    workout.entry_mode = request.entry_mode
    workout.revision = 0
    workout.history_request_fingerprint = _history_request_fingerprint(request)
    loads = iter([None, workout])

    async def load_by_client_uuid(*args, **kwargs):
        return next(loads)

    async def validate_exercises(*args, **kwargs):
        return {9: workout.exercises[0].exercise}

    class FakeDb:
        rollback_count = 0

        def add(self, value):
            return None

        async def flush(self):
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("INSERT", {}, Exception("unique race"))

        async def rollback(self):
            self.rollback_count += 1

    monkeypatch.setattr(
        workout_history_service,
        "_load_workout_by_client_uuid",
        load_by_client_uuid,
    )
    monkeypatch.setattr(workout_history_service, "_validate_exercises", validate_exercises)
    db = FakeDb()

    result = await create_history_workout(db, 1, request)

    assert result is workout
    assert db.rollback_count == 1
