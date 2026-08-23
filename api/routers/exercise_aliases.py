"""Authenticated API for source-aware exercise import aliases."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.exercise_aliases import (
    ExerciseAliasExerciseItem,
    ExerciseAliasResponse,
    ExerciseAliasUpsertRequest,
)
from api.services.app_user_service import get_current_app_user
from api.services.import_service import (
    ExerciseAliasIdentityCollisionError,
    ExerciseAliasNotAccessibleError,
    save_alias,
)
from api.services.models import AppUser


router = APIRouter(prefix="/exercises", tags=["exercises"])


@router.put("/import-alias", response_model=ExerciseAliasResponse)
async def put_exercise_import_alias(
    payload: ExerciseAliasUpsertRequest,
    db: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
) -> ExerciseAliasResponse:
    """Persist one external alias against an exercise visible to the user."""
    try:
        alias = await save_alias(
            session=db,
            app_user_id=app_user.id,
            source=payload.source,
            external_name=payload.external_name,
            exercise_id=payload.exercise_id,
        )
    except ExerciseAliasNotAccessibleError as exc:
        # Unknown and foreign-private IDs intentionally share one response.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Exercise not found",
        ) from exc
    except ExerciseAliasIdentityCollisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Alias identity conflict",
        ) from exc

    exercise = alias.exercise
    await db.commit()
    return ExerciseAliasResponse(
        source=alias.source,
        external_name=alias.external_name,
        normalized_external_name=alias.normalized_external_name,
        exercise_id=exercise.id,
        exercise=ExerciseAliasExerciseItem(
            id=exercise.id,
            name=exercise.name,
            category=exercise.category,
            main_muscle_group=exercise.main_muscle_group,
            secondary_muscle_groups=exercise.secondary_muscle_groups or [],
            equipment_needed=exercise.equipment_needed or [],
            source=exercise.source,
        ),
    )
