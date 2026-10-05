from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.schemas.workout_history import (
    WorkoutHistoryDraft,
    WorkoutHistoryExerciseDraft,
    WorkoutHistoryListItem,
    WorkoutHistoryServerSnapshot,
    WorkoutHistorySetDraft,
)
from api.services.exercise_utils import get_base_exercise_query
from api.services.anomaly_guard import check_set, resolve_is_anomalous
from api.services.anomaly_stats import load_exercise_stats
from api.services.models import (
    Exercise,
    WorkoutRecalculationOperation,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)


class WorkoutHistoryError(Exception):
    pass


class WorkoutHistoryNotFound(WorkoutHistoryError):
    pass


class WorkoutHistoryNotFinished(WorkoutHistoryError):
    pass


class WorkoutHistoryImmutableField(WorkoutHistoryError):
    def __init__(self, field: str):
        self.field = field
        super().__init__(field)


class WorkoutHistoryInvalidExercise(WorkoutHistoryError):
    def __init__(self, exercise_id: int):
        self.exercise_id = exercise_id
        super().__init__(str(exercise_id))


class WorkoutHistoryRevisionConflict(WorkoutHistoryError):
    def __init__(
        self,
        server_revision: int,
        server_draft: WorkoutHistoryServerSnapshot,
    ):
        self.server_revision = server_revision
        self.server_draft = server_draft
        super().__init__(str(server_revision))


class WorkoutHistoryIdempotencyConflict(WorkoutHistoryError):
    def __init__(
        self,
        server_workout_id: int,
        server_draft: WorkoutHistoryServerSnapshot,
    ):
        self.server_workout_id = server_workout_id
        self.server_draft = server_draft
        super().__init__(server_draft.client_uuid)


def _history_request_fingerprint(draft: WorkoutHistoryDraft) -> str:
    canonical = json.dumps(
        draft.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_exact_history_replay(
    existing: WorkoutSession,
    request_fingerprint: str,
) -> WorkoutSession:
    if (
        existing.status == "finished"
        and existing.entry_mode in {"manual", "screenshot_import"}
        and existing.history_request_fingerprint == request_fingerprint
    ):
        return existing
    raise WorkoutHistoryIdempotencyConflict(
        existing.id,
        draft_from_workout(existing),
    )


def _history_set_state(item: WorkoutHistorySetDraft) -> tuple[object, ...]:
    return (
        item.client_uuid,
        item.parent_client_uuid,
        item.set_number,
        item.set_type,
        item.weight,
        item.reps,
        item.effort_level,
        item.notes,
        item.is_completed,
    )


def _history_exercise_state(item: WorkoutHistoryExerciseDraft) -> tuple[object, ...]:
    return (
        item.client_uuid,
        item.exercise_id,
        item.order_index,
        item.superset_group,
        item.notes,
        tuple(
            _history_set_state(workout_set)
            for workout_set in sorted(
                item.sets,
                key=lambda value: (value.set_number, value.client_uuid),
            )
        ),
    )


def _history_mutable_state(draft: WorkoutHistoryDraft) -> tuple[object, ...]:
    return (
        draft.entry_mode,
        draft.started_at,
        draft.finished_at,
        draft.notes,
        draft.session_rpe,
        tuple(
            _history_exercise_state(exercise)
            for exercise in sorted(
                draft.exercises,
                key=lambda value: (value.order_index, value.client_uuid),
            )
        ),
    )


def _is_exact_history_correction_replay(
    workout: WorkoutSession,
    draft: WorkoutHistoryDraft,
) -> bool:
    return (
        workout.revision == draft.base_revision + 1
        and _history_mutable_state(draft_from_workout(workout))
        == _history_mutable_state(draft)
    )


def _workout_options():
    return selectinload(WorkoutSession.exercises).options(
        selectinload(WorkoutSessionExercise.exercise),
        selectinload(WorkoutSessionExercise.sets),
    )


async def _load_owned_workout(
    db: AsyncSession,
    user_id: int,
    workout_id: int,
    *,
    for_update: bool = False,
) -> WorkoutSession | None:
    statement = (
        select(WorkoutSession)
        .where(
            WorkoutSession.id == workout_id,
            WorkoutSession.app_user_id == user_id,
        )
        .options(_workout_options())
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def _validate_exercises(
    db: AsyncSession,
    user_id: int,
    draft: WorkoutHistoryDraft,
) -> dict[int, Exercise]:
    exercise_ids = {item.exercise_id for item in draft.exercises}
    if not exercise_ids:
        return {}
    exercises = list(
        (
            await db.execute(
                get_base_exercise_query(user_id).where(Exercise.id.in_(exercise_ids))
            )
        )
        .scalars()
        .all()
    )
    by_id = {item.id: item for item in exercises}
    missing = exercise_ids - set(by_id)
    if missing:
        raise WorkoutHistoryInvalidExercise(min(missing))
    return by_id


def _apply_set_fields(
    target: WorkoutSessionSet,
    source,
    stats,
) -> None:
    target.client_uuid = source.client_uuid
    target.set_number = source.set_number
    target.set_type = source.set_type
    target.weight = source.weight
    target.reps = source.reps
    target.effort_level = source.effort_level
    target.notes = source.notes
    target.is_completed = source.is_completed
    verdict = check_set(
        float(source.weight) if source.weight is not None else None,
        source.reps,
        source.set_type,
        stats,
    )
    target.is_anomalous = resolve_is_anomalous(verdict, confirmed=False)


def _apply_set_parent_links(
    source_sets: list[WorkoutHistorySetDraft],
    target_sets_by_client_uuid: dict[str, WorkoutSessionSet],
) -> None:
    for source_set in source_sets:
        target_set = target_sets_by_client_uuid[source_set.client_uuid]
        parent_uuid = source_set.parent_client_uuid
        target_set.parent_set_id = (
            target_sets_by_client_uuid[parent_uuid].id
            if parent_uuid is not None
            else None
        )


def _new_session_exercise(
    workout_id: int,
    source: WorkoutHistoryExerciseDraft,
    exercise: Exercise,
) -> WorkoutSessionExercise:
    item = WorkoutSessionExercise(
        workout_session_id=workout_id,
        client_uuid=source.client_uuid,
        exercise_id=source.exercise_id,
        order_index=source.order_index,
        superset_group=source.superset_group,
        notes=source.notes,
        sets=[],
    )
    item.exercise = exercise
    return item


async def _replace_exercises(
    db: AsyncSession,
    workout: WorkoutSession,
    draft: WorkoutHistoryDraft,
    exercise_by_id: dict[int, Exercise],
) -> None:
    # Existing production indexes are immediate. Move current order and set
    # numbers out of the final range before replacing the snapshot.
    if workout.exercises:
        order_offset = max(
            [item.order_index for item in workout.exercises]
            + [item.order_index for item in draft.exercises]
        ) + len(workout.exercises) + 1
        for position, item in enumerate(workout.exercises):
            item.order_index = order_offset + position
            if item.sets:
                set_offset = max(
                    [workout_set.set_number for workout_set in item.sets]
                    + [workout_set.set_number for source in draft.exercises
                       if source.client_uuid == item.client_uuid
                       for workout_set in source.sets],
                    default=0,
                ) + len(item.sets) + 1
                for set_position, workout_set in enumerate(item.sets):
                    workout_set.set_number = set_offset + set_position
        await db.flush()

    existing_exercises = {
        item.client_uuid or f"server-exercise-{item.id}": item
        for item in workout.exercises
    }
    retained_exercise_uuids = {item.client_uuid for item in draft.exercises}

    for existing in list(workout.exercises):
        existing_key = existing.client_uuid or f"server-exercise-{existing.id}"
        if existing_key not in retained_exercise_uuids:
            await db.delete(existing)

    for source_exercise in draft.exercises:
        stats = await load_exercise_stats(
            db, workout.app_user_id, source_exercise.exercise_id
        )
        target_exercise = existing_exercises.get(source_exercise.client_uuid)
        if target_exercise is None:
            target_exercise = _new_session_exercise(
                workout.id,
                source_exercise,
                exercise_by_id[source_exercise.exercise_id],
            )
            db.add(target_exercise)
            await db.flush()
        else:
            # Только факты. prescription/live_prescription, target_sets и
            # phase/block context остаются ровно теми, что видел пользователь.
            target_exercise.exercise_id = source_exercise.exercise_id
            target_exercise.exercise = exercise_by_id[source_exercise.exercise_id]
            target_exercise.order_index = source_exercise.order_index
            target_exercise.superset_group = source_exercise.superset_group
            target_exercise.notes = source_exercise.notes
            if target_exercise.client_uuid is None:
                target_exercise.client_uuid = source_exercise.client_uuid

        existing_sets = {
            item.client_uuid or f"server-set-{item.id}": item
            for item in target_exercise.sets
        }
        retained_set_uuids = {item.client_uuid for item in source_exercise.sets}
        for existing_set in list(target_exercise.sets):
            existing_set_key = (
                existing_set.client_uuid or f"server-set-{existing_set.id}"
            )
            if existing_set_key not in retained_set_uuids:
                await db.delete(existing_set)

        target_sets_by_client_uuid: dict[str, WorkoutSessionSet] = {}
        for source_set in source_exercise.sets:
            target_set = existing_sets.get(source_set.client_uuid)
            if target_set is None:
                target_set = WorkoutSessionSet(
                    workout_session_exercise_id=target_exercise.id,
                    client_uuid=source_set.client_uuid,
                    set_number=source_set.set_number,
                )
                db.add(target_set)
            elif target_set.client_uuid is None:
                target_set.client_uuid = source_set.client_uuid
            _apply_set_fields(target_set, source_set, stats)
            target_sets_by_client_uuid[source_set.client_uuid] = target_set

        # IDs for newly created root/drop rows are needed before ancestry can
        # be resolved. The public contract uses stable client UUIDs so the
        # service never depends on a client knowing server persistence IDs.
        await db.flush()
        _apply_set_parent_links(
            source_exercise.sets,
            target_sets_by_client_uuid,
        )


def _enqueue_recalculation(
    db: AsyncSession,
    workout_id: int,
    revision: int,
    exercise_ids: set[int],
) -> WorkoutRecalculationOperation:
    operation = WorkoutRecalculationOperation(
        workout_id=workout_id,
        revision=revision,
        status="pending",
        exercise_ids=sorted(exercise_ids),
    )
    db.add(operation)
    return operation


async def _reload_workout(
    db: AsyncSession,
    user_id: int,
    workout_id: int,
) -> WorkoutSession:
    db.expunge_all()
    workout = await _load_owned_workout(db, user_id, workout_id)
    if workout is None:
        raise WorkoutHistoryNotFound
    return workout


async def create_history_workout(
    db: AsyncSession,
    user_id: int,
    draft: WorkoutHistoryDraft,
) -> WorkoutSession:
    if draft.entry_mode == "live":
        raise WorkoutHistoryImmutableField("entry_mode")
    if draft.base_revision != 0:
        raise WorkoutHistoryImmutableField("base_revision")

    request_fingerprint = _history_request_fingerprint(draft)
    existing = await _load_workout_by_client_uuid(db, user_id, draft.client_uuid)
    if existing is not None:
        return _require_exact_history_replay(existing, request_fingerprint)

    try:
        exercise_by_id = await _validate_exercises(db, user_id, draft)
        workout = WorkoutSession(
            app_user_id=user_id,
            client_uuid=draft.client_uuid,
            source="free",
            status="finished",
            entry_mode=draft.entry_mode,
            revision=0,
            history_request_fingerprint=request_fingerprint,
            notes=draft.notes,
            session_rpe=draft.session_rpe,
            session_rpe_at=draft.finished_at if draft.session_rpe is not None else None,
            started_at=draft.started_at,
            finished_at=draft.finished_at,
            exercises=[],
        )
        db.add(workout)
        await db.flush()
        await _replace_exercises(db, workout, draft, exercise_by_id)
        _enqueue_recalculation(
            db,
            workout.id,
            workout.revision,
            {item.exercise_id for item in draft.exercises},
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await _load_workout_by_client_uuid(db, user_id, draft.client_uuid)
        if existing is not None:
            return _require_exact_history_replay(existing, request_fingerprint)
        raise
    except Exception:
        await db.rollback()
        raise
    return await _reload_workout(db, user_id, workout.id)


async def replace_history_workout(
    db: AsyncSession,
    user_id: int,
    workout_id: int,
    draft: WorkoutHistoryDraft,
) -> WorkoutSession:
    try:
        workout = await _load_owned_workout(
            db, user_id, workout_id, for_update=True
        )
        if workout is None:
            raise WorkoutHistoryNotFound
        if workout.status != "finished":
            raise WorkoutHistoryNotFinished
        if workout.revision != draft.base_revision:
            if _is_exact_history_correction_replay(workout, draft):
                return workout
            raise WorkoutHistoryRevisionConflict(
                workout.revision, draft_from_workout(workout)
            )
        if workout.entry_mode != draft.entry_mode:
            raise WorkoutHistoryImmutableField("entry_mode")

        exercise_by_id = await _validate_exercises(db, user_id, draft)
        affected_exercise_ids = {
            item.exercise_id for item in workout.exercises
        } | {item.exercise_id for item in draft.exercises}
        workout.started_at = draft.started_at
        workout.finished_at = draft.finished_at
        workout.notes = draft.notes
        workout.session_rpe = draft.session_rpe
        workout.session_rpe_at = (
            draft.finished_at if draft.session_rpe is not None else None
        )
        await _replace_exercises(db, workout, draft, exercise_by_id)
        workout.revision += 1
        workout.edited_at = datetime.now(timezone.utc)
        _enqueue_recalculation(
            db, workout.id, workout.revision, affected_exercise_ids
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return await _reload_workout(db, user_id, workout_id)


def _draft_sets_from_exercise(
    exercise: WorkoutSessionExercise,
) -> list[dict[str, object]]:
    client_uuid_by_id = {
        workout_set.id: workout_set.client_uuid or f"server-set-{workout_set.id}"
        for workout_set in exercise.sets
    }
    return [
        {
            "client_uuid": client_uuid_by_id[workout_set.id],
            "parent_client_uuid": (
                client_uuid_by_id.get(workout_set.parent_set_id)
                if workout_set.parent_set_id is not None
                else None
            ),
            "set_number": workout_set.set_number,
            "set_type": workout_set.set_type,
            "weight": workout_set.weight,
            "reps": workout_set.reps,
            "effort_level": workout_set.effort_level,
            "notes": workout_set.notes,
            "is_completed": workout_set.is_completed,
        }
        for workout_set in exercise.sets
    ]


def draft_from_workout(workout: WorkoutSession) -> WorkoutHistoryServerSnapshot:
    return WorkoutHistoryServerSnapshot(
        client_uuid=workout.client_uuid or f"server-workout-{workout.id}",
        base_revision=workout.revision,
        entry_mode=workout.entry_mode,
        started_at=workout.started_at,
        finished_at=workout.finished_at,
        notes=workout.notes,
        session_rpe=workout.session_rpe,
        exercises=[
            {
                "client_uuid": item.client_uuid or f"server-exercise-{item.id}",
                "exercise_id": item.exercise_id,
                "exercise_name": item.exercise.name if item.exercise is not None else None,
                "order_index": item.order_index,
                "superset_group": item.superset_group,
                "notes": item.notes,
                "sets": _draft_sets_from_exercise(item),
            }
            for item in workout.exercises
        ],
    )


async def _load_workout_by_client_uuid(
    db: AsyncSession,
    user_id: int,
    client_uuid: str,
) -> WorkoutSession | None:
    return (
        await db.execute(
            select(WorkoutSession)
            .where(
                WorkoutSession.app_user_id == user_id,
                WorkoutSession.client_uuid == client_uuid,
            )
            .options(_workout_options())
        )
    ).scalar_one_or_none()


async def list_history_workouts(
    db: AsyncSession,
    user_id: int,
) -> list[WorkoutHistoryListItem]:
    workouts = list(
        (
            await db.execute(
                select(WorkoutSession)
                .where(
                    WorkoutSession.app_user_id == user_id,
                    WorkoutSession.status == "finished",
                    WorkoutSession.finished_at.is_not(None),
                )
                .options(_workout_options())
                .order_by(WorkoutSession.started_at.desc(), WorkoutSession.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        WorkoutHistoryListItem(
            id=workout.id,
            client_uuid=workout.client_uuid,
            source=workout.source,
            entry_mode=workout.entry_mode,
            revision=workout.revision,
            started_at=workout.started_at,
            finished_at=workout.finished_at,
            edited_at=workout.edited_at,
            notes=workout.notes,
            session_rpe=workout.session_rpe,
            exercises_count=len(workout.exercises),
            sets_count=sum(len(item.sets) for item in workout.exercises),
        )
        for workout in workouts
    ]
