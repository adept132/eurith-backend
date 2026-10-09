"""Owner scoped warmup preference endpoints."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.gym_profiles import ExerciseSource
from api.schemas.warmups import WarmupPreferencePayload, WarmupPreferenceView
from api.services.app_user_service import get_current_app_user
from api.services.models import AppUser, ExerciseWarmupPreference
from api.services.warmup_preferences import (
    ExerciseWarmupPreferenceNotFound,
    ExerciseWarmupPreferenceRevisionConflict,
    list_warmup_preferences,
    put_exercise_warmup_preference,
)

router = APIRouter(tags=["warmups"])


def _view(row: ExerciseWarmupPreference) -> WarmupPreferenceView:
    return WarmupPreferenceView(id=row.id, exercise_source=row.exercise_source,
                                exercise_id=row.exercise_id, override=row.override, revision=row.revision)


@router.get("/warmup-preferences", response_model=list[WarmupPreferenceView])
async def get_preferences(
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> list[WarmupPreferenceView]:
    return [_view(row) for row in await list_warmup_preferences(db, current_user.id)]


@router.put("/warmup-preferences/{source}/{exercise_id}", response_model=WarmupPreferenceView)
async def put_preference(
    source: ExerciseSource,
    exercise_id: Annotated[int, Path(gt=0)],
    payload: WarmupPreferencePayload,
    current_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
) -> WarmupPreferenceView:
    try:
        row = await put_exercise_warmup_preference(db, current_user.id, source, exercise_id, payload)
    except ExerciseWarmupPreferenceNotFound as error:
        raise HTTPException(status_code=404, detail="Exercise or warmup preference not found") from error
    except ExerciseWarmupPreferenceRevisionConflict as error:
        raise HTTPException(status_code=409, detail={
            "message": str(error), "current": _view(error.current).model_dump(mode="json"),
            "revision": error.current.revision,
        }) from error
    return _view(row)
