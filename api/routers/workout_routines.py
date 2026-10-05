from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_db
from api.schemas.workout_routines import (
    SaveWorkoutAsRoutineRequest,
    WorkoutRoutineCreate,
    WorkoutRoutineExerciseWrite,
    WorkoutRoutineRead,
    WorkoutRoutineUpdate,
)
from api.services.app_user_service import get_current_app_user
from api.services.models import (
    Exercise,
    WorkoutRoutine,
    WorkoutRoutineExercise,
    WorkoutSession,
    WorkoutSessionExercise,
)
from api.services.workout_structure import extract_workout_structure
from api.services.routine_start import (
    ActiveWorkoutExists,
    RoutineNotFound,
    start_routine,
)
from api.schemas.workouts import WorkoutSessionDetailResponse
from datetime import datetime, timezone


router = APIRouter(tags=["workout-routines"])


def _routine_query():
    return select(WorkoutRoutine).options(
        selectinload(WorkoutRoutine.exercises).selectinload(
            WorkoutRoutineExercise.exercise
        )
    )


async def _owned_routine(
    db: AsyncSession, user_id: int, routine_id: int, *, for_update: bool = False
) -> WorkoutRoutine:
    query = _routine_query().where(
        WorkoutRoutine.id == routine_id,
        WorkoutRoutine.app_user_id == user_id,
    )
    if for_update:
        query = query.with_for_update()
    routine = (
        await db.execute(query)
    ).scalar_one_or_none()
    if routine is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    return routine


async def _validate_exercises(
    db: AsyncSession, user_id: int, exercises: list[WorkoutRoutineExerciseWrite]
) -> None:
    exercise_ids = {item.exercise_id for item in exercises}
    available = set(
        (
            await db.execute(
                select(Exercise.id).where(
                    Exercise.id.in_(exercise_ids),
                    or_(
                        Exercise.app_user_id.is_(None),
                        Exercise.app_user_id == user_id,
                    ),
                )
            )
        ).scalars()
    )
    invalid = sorted(exercise_ids - available)
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_routine_exercise", "exercise_ids": invalid},
        )


def _exercise_model(item: WorkoutRoutineExerciseWrite) -> WorkoutRoutineExercise:
    return WorkoutRoutineExercise(**item.model_dump())


async def _reload(db: AsyncSession, routine_id: int) -> WorkoutRoutine:
    return (
        await db.execute(
            _routine_query()
            .where(WorkoutRoutine.id == routine_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _create_routine(
    db: AsyncSession,
    user_id: int,
    payload: WorkoutRoutineCreate,
) -> WorkoutRoutine:
    await _validate_exercises(db, user_id, payload.exercises)
    client_uuid = payload.client_uuid or str(uuid.uuid4())
    duplicate = await db.scalar(
        select(WorkoutRoutine.id).where(
            WorkoutRoutine.app_user_id == user_id,
            WorkoutRoutine.client_uuid == client_uuid,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "routine_client_uuid_conflict"},
        )
    routine = WorkoutRoutine(
        app_user_id=user_id,
        client_uuid=client_uuid,
        name=payload.name,
        notes=payload.notes,
        sort_order=payload.sort_order,
        exercises=[_exercise_model(item) for item in payload.exercises],
    )
    db.add(routine)
    await db.commit()
    return await _reload(db, routine.id)


@router.get("/workout-routines", response_model=list[WorkoutRoutineRead])
async def list_workout_routines(
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    return (
        await db.execute(
            _routine_query()
            .where(WorkoutRoutine.app_user_id == current_app_user.id)
            .order_by(WorkoutRoutine.sort_order, WorkoutRoutine.created_at, WorkoutRoutine.id)
        )
    ).scalars().all()


@router.post(
    "/workout-routines",
    response_model=WorkoutRoutineRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_workout_routine(
    payload: WorkoutRoutineCreate,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    return await _create_routine(db, current_app_user.id, payload)


@router.get("/workout-routines/{routine_id}", response_model=WorkoutRoutineRead)
async def get_workout_routine(
    routine_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    return await _owned_routine(db, current_app_user.id, routine_id)


@router.patch("/workout-routines/{routine_id}", response_model=WorkoutRoutineRead)
async def update_workout_routine(
    routine_id: int,
    payload: WorkoutRoutineUpdate,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    routine = await _owned_routine(
        db, current_app_user.id, routine_id, for_update=True
    )
    if routine.revision != payload.base_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "routine_revision_conflict",
                "routine": WorkoutRoutineRead.model_validate(routine).model_dump(mode="json"),
            },
        )
    fields = payload.model_fields_set
    if "name" in fields:
        routine.name = payload.name
    if "notes" in fields:
        routine.notes = payload.notes
    if "sort_order" in fields:
        routine.sort_order = payload.sort_order
    if payload.exercises is not None:
        await _validate_exercises(db, current_app_user.id, payload.exercises)
        routine.exercises.clear()
        # The unique (routine_id, order_index) constraint is immediate. Flush
        # deletions before inserting the replacement rows with the same order.
        await db.flush()
        routine.exercises = [_exercise_model(item) for item in payload.exercises]
    routine.revision += 1
    routine.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await _reload(db, routine.id)


@router.delete(
    "/workout-routines/{routine_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_workout_routine(
    routine_id: int,
    base_revision: int = Query(ge=0),
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    routine = await _owned_routine(
        db, current_app_user.id, routine_id, for_update=True
    )
    if routine.revision != base_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "routine_revision_conflict",
                "routine": WorkoutRoutineRead.model_validate(routine).model_dump(mode="json"),
            },
        )
    await db.delete(routine)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/workout-routines/{routine_id}/start",
    response_model=WorkoutSessionDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_workout_routine(
    routine_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    try:
        return await start_routine(
            db, current_app_user, routine_id, datetime.now(timezone.utc)
        )
    except RoutineNotFound as exc:
        raise HTTPException(status_code=404, detail="Routine not found") from exc
    except ActiveWorkoutExists as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Active workout already exists",
        ) from exc


@router.post(
    "/workouts/{workout_id}/save-as-routine",
    response_model=WorkoutRoutineRead,
    status_code=status.HTTP_201_CREATED,
)
async def save_workout_as_routine(
    workout_id: int,
    payload: SaveWorkoutAsRoutineRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    workout = (
        await db.execute(
            select(WorkoutSession)
            .where(
                WorkoutSession.id == workout_id,
                WorkoutSession.app_user_id == current_app_user.id,
            )
            .options(
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.sets
                )
            )
        )
    ).scalar_one_or_none()
    if workout is None:
        raise HTTPException(status_code=404, detail="Workout not found")
    structure = extract_workout_structure(
        workout, persistent_note_ids=payload.persistent_note_ids
    )
    if not structure.exercises:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "routine_requires_exercises"},
        )
    create_payload = WorkoutRoutineCreate(
        client_uuid=payload.client_uuid,
        name=payload.name,
        notes=structure.notes,
        sort_order=payload.sort_order,
        exercises=[
            WorkoutRoutineExerciseWrite(**item.model_dump())
            for item in structure.exercises
        ],
    )
    return await _create_routine(db, current_app_user.id, create_payload)
