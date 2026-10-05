from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.workout_history import (
    WorkoutHistoryDraft,
    WorkoutHistoryListResponse,
    WorkoutHistorySaveResponse,
)
from api.services.app_user_service import get_current_app_user
from api.services.workout_history import (
    WorkoutHistoryImmutableField,
    WorkoutHistoryIdempotencyConflict,
    WorkoutHistoryInvalidExercise,
    WorkoutHistoryNotFinished,
    WorkoutHistoryNotFound,
    WorkoutHistoryRevisionConflict,
    create_history_workout,
    list_history_workouts,
    replace_history_workout,
)


router = APIRouter(tags=["workout-history"])


def _translate_history_error(exc: Exception) -> HTTPException:
    if isinstance(exc, WorkoutHistoryNotFound):
        return HTTPException(status_code=404, detail="Workout not found")
    if isinstance(exc, WorkoutHistoryNotFinished):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "workout_not_finished"},
        )
    if isinstance(exc, WorkoutHistoryImmutableField):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "immutable_history_field", "field": exc.field},
        )
    if isinstance(exc, WorkoutHistoryInvalidExercise):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_history_exercise", "exercise_id": exc.exercise_id},
        )
    if isinstance(exc, WorkoutHistoryRevisionConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "history_revision_conflict",
                "server_revision": exc.server_revision,
                "server_draft": exc.server_draft.model_dump(mode="json"),
            },
        )
    if isinstance(exc, WorkoutHistoryIdempotencyConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "history_idempotency_conflict",
                "server_workout_id": exc.server_workout_id,
                "server_revision": exc.server_draft.base_revision,
                "server_draft": exc.server_draft.model_dump(mode="json"),
            },
        )
    raise exc


@router.get("/workouts/history", response_model=WorkoutHistoryListResponse)
async def get_workout_history(
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    return WorkoutHistoryListResponse(
        workouts=await list_history_workouts(db, current_app_user.id)
    )


@router.post(
    "/workouts/history",
    response_model=WorkoutHistorySaveResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workout_history(
    payload: WorkoutHistoryDraft,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    try:
        workout = await create_history_workout(db, current_app_user.id, payload)
    except Exception as exc:
        raise _translate_history_error(exc) from exc
    return WorkoutHistorySaveResponse(workout=workout, recalculation_status="pending")


@router.patch(
    "/workouts/{workout_id}/history",
    response_model=WorkoutHistorySaveResponse,
)
async def correct_workout_history(
    workout_id: int,
    payload: WorkoutHistoryDraft,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    try:
        workout = await replace_history_workout(
            db, current_app_user.id, workout_id, payload
        )
    except Exception as exc:
        raise _translate_history_error(exc) from exc
    return WorkoutHistorySaveResponse(workout=workout, recalculation_status="pending")
