from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.carry_over import (
    CarryOverDecisionRequest,
    CarryOverPreviewRequest,
    CarryOverResponse,
)
from api.services.app_user_service import get_current_app_user
from api.services.carry_over import (
    CarryOverDecisionConflict,
    CarryOverIdempotencyConflict,
    CarryOverSourceNotFound,
    InvalidUnfinishedSets,
    StaleCarryOverPreview,
    apply_carry_over_preview,
    create_carry_over_preview,
    dismiss_carry_over_preview,
)


router = APIRouter(prefix="/workouts", tags=["workouts"])


def _workflow_error(error: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": error})


@router.post(
    "/{workout_id}/carry-over/preview",
    response_model=CarryOverResponse,
)
async def preview_workout_carry_over(
    workout_id: int,
    body: CarryOverPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    unfinished = {
        item.source_exercise_id: item.requested_sets
        for item in body.unfinished_sets
    }
    try:
        return await create_carry_over_preview(
            db,
            current_app_user.id,
            workout_id,
            unfinished,
            body.idempotency_key,
            datetime.now(timezone.utc),
        )
    except CarryOverSourceNotFound as exc:
        raise _workflow_error("source_not_found", status.HTTP_404_NOT_FOUND) from exc
    except InvalidUnfinishedSets as exc:
        raise _workflow_error(
            "invalid_unfinished_sets", status.HTTP_422_UNPROCESSABLE_ENTITY
        ) from exc
    except CarryOverIdempotencyConflict as exc:
        raise _workflow_error(
            "idempotency_conflict", status.HTTP_409_CONFLICT
        ) from exc
    except CarryOverDecisionConflict as exc:
        raise _workflow_error(
            "carry_over_already_decided", status.HTTP_409_CONFLICT
        ) from exc


@router.post(
    "/{workout_id}/carry-over/apply",
    response_model=CarryOverResponse,
)
async def apply_workout_carry_over(
    workout_id: int,
    body: CarryOverDecisionRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    try:
        return await apply_carry_over_preview(
            db,
            current_app_user.id,
            workout_id,
            body.preview_token,
            datetime.now(timezone.utc),
        )
    except CarryOverSourceNotFound as exc:
        raise _workflow_error("source_not_found", status.HTTP_404_NOT_FOUND) from exc
    except InvalidUnfinishedSets as exc:
        raise _workflow_error(
            "invalid_unfinished_sets", status.HTTP_422_UNPROCESSABLE_ENTITY
        ) from exc
    except CarryOverDecisionConflict as exc:
        raise _workflow_error(
            "carry_over_already_decided", status.HTTP_409_CONFLICT
        ) from exc
    except StaleCarryOverPreview as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "stale_preview", "preview": exc.preview.model_dump(mode="json")},
        ) from exc


@router.post(
    "/{workout_id}/carry-over/dismiss",
    response_model=CarryOverResponse,
)
async def dismiss_workout_carry_over(
    workout_id: int,
    body: CarryOverDecisionRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    try:
        return await dismiss_carry_over_preview(
            db,
            current_app_user.id,
            workout_id,
            body.preview_token,
        )
    except CarryOverSourceNotFound as exc:
        raise _workflow_error("source_not_found", status.HTTP_404_NOT_FOUND) from exc
    except CarryOverDecisionConflict as exc:
        raise _workflow_error(
            "carry_over_already_decided", status.HTTP_409_CONFLICT
        ) from exc
