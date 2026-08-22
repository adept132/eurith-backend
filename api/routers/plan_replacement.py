from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.plan_replacement import (
    PlanReplacementApplyRequest,
    PlanReplacementPreview,
    PlanReplacementPreviewRequest,
    PlanReplacementResult,
    PlanReplacementSourceKind,
)
from api.services.app_user_service import get_current_app_user
from api.services.plan_replacement import (
    EmptyReplacementStructure,
    InvalidReplacementExercise,
    NoReplaceablePlanDates,
    ReplacementSourceNotFound,
    StaleReplacementPreview,
    TargetPlanNotFound,
    PlanReplacementIdempotencyConflict,
    apply_replacement,
    preview_replacement,
)


router = APIRouter(tags=["plan-replacement"])


def _replacement_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ReplacementSourceNotFound):
        return HTTPException(status_code=404, detail={"error": "source_not_found"})
    if isinstance(exc, TargetPlanNotFound):
        return HTTPException(status_code=404, detail={"error": "target_plan_not_found"})
    if isinstance(exc, EmptyReplacementStructure):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "empty_replacement_structure"},
        )
    if isinstance(exc, InvalidReplacementExercise):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "invalid_replacement_exercise",
                "exercise_ids": exc.exercise_ids,
            },
        )
    if isinstance(exc, StaleReplacementPreview):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "stale_preview",
                "preview": exc.preview.model_dump(mode="json"),
            },
        )
    if isinstance(exc, NoReplaceablePlanDates):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "no_replaceable_plan_dates"},
        )
    if isinstance(exc, PlanReplacementIdempotencyConflict):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "idempotency_conflict"},
        )
    raise exc


async def _preview(
    *,
    source_kind: PlanReplacementSourceKind,
    source_id: int,
    payload: PlanReplacementPreviewRequest,
    db: AsyncSession,
    user_id: int,
) -> PlanReplacementPreview:
    try:
        return await preview_replacement(
            db,
            user_id,
            (source_kind, source_id),
            payload.target_plan_id,
            date.today(),
        )
    except Exception as exc:
        raise _replacement_error(exc) from exc


@router.post(
    "/workouts/{workout_id}/plan-replacement/preview",
    response_model=PlanReplacementPreview,
)
async def preview_workout_plan_replacement(
    workout_id: int,
    payload: PlanReplacementPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    return await _preview(
        source_kind="workout",
        source_id=workout_id,
        payload=payload,
        db=db,
        user_id=current_app_user.id,
    )


@router.post(
    "/workout-routines/{routine_id}/plan-replacement/preview",
    response_model=PlanReplacementPreview,
)
async def preview_routine_plan_replacement(
    routine_id: int,
    payload: PlanReplacementPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    return await _preview(
        source_kind="routine",
        source_id=routine_id,
        payload=payload,
        db=db,
        user_id=current_app_user.id,
    )


async def _apply(
    *,
    source_kind: PlanReplacementSourceKind,
    source_id: int,
    payload: PlanReplacementApplyRequest,
    db: AsyncSession,
    user_id: int,
) -> PlanReplacementResult:
    try:
        return await apply_replacement(
            db,
            user_id,
            (source_kind, source_id),
            payload.target_plan_id,
            date.today(),
            payload.preview_token,
            payload.idempotency_key,
        )
    except Exception as exc:
        raise _replacement_error(exc) from exc


@router.post(
    "/workouts/{workout_id}/plan-replacement/apply",
    response_model=PlanReplacementResult,
)
async def apply_workout_plan_replacement(
    workout_id: int,
    payload: PlanReplacementApplyRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    return await _apply(
        source_kind="workout",
        source_id=workout_id,
        payload=payload,
        db=db,
        user_id=current_app_user.id,
    )


@router.post(
    "/workout-routines/{routine_id}/plan-replacement/apply",
    response_model=PlanReplacementResult,
)
async def apply_routine_plan_replacement(
    routine_id: int,
    payload: PlanReplacementApplyRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    return await _apply(
        source_kind="routine",
        source_id=routine_id,
        payload=payload,
        db=db,
        user_id=current_app_user.id,
    )
