"""Authenticated versioned analytics; legacy progress resources remain separate."""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from api.services.models import AppUserProfile
from api.deps import get_db
from api.services.app_user_service import get_current_app_user
from api.services.analytics import service, state
from api.services.analytics.config import analytics_enabled
from api.services.analytics.publication import coherent_read
from api.services.analytics.compact import pack_resource
from api.schemas.analytics_compact import AnalyticsCompactV1
from api.schemas.analytics import (AnalyticsCapabilities, AnalyticsUnavailable, ExerciseAnalytics,
    MuscleAnalytics, BlockAnalytics, BlockComparison, PeriodQuery, GymQuery, CompareQuery)


async def private_response(request: Request, response: Response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Vary'] = 'Authorization'
    keys = list(request.query_params.keys())
    if any(len(request.query_params.getlist(key)) != 1 for key in keys):
        raise HTTPException(422, detail='duplicate_query_parameter')


router = APIRouter(prefix='/progress/analytics', tags=['analytics'], dependencies=[Depends(private_response)])
User = Annotated[object, Depends(get_current_app_user)]
DB = Annotated[AsyncSession, Depends(get_db)]
PositiveID = Annotated[int, Path(gt=0)]


async def _read(db, user_id, assemble):
    try:
        return await coherent_read(db, user_id, assemble)
    except ValueError as exc:
        reason = str(exc)
        if reason in {'exercise_not_found', 'block_not_found', 'preview_workout_not_found'}:
            raise HTTPException(404, detail=reason) from exc
        if reason in {'invalid_period', 'invalid_period_bounds', 'unknown_block_end',
                      'comparison_requires_closed_blocks', 'preview_workout_not_active'}:
            raise HTTPException(422, detail=reason) from exc
        raise


@router.get('/capabilities', response_model=AnalyticsCapabilities)
async def capabilities(request: Request, user: User, db: DB):
    if request.query_params:
        raise HTTPException(422, detail='unexpected_query_parameter')
    current = await state.readiness(db, user.id)
    zone = await db.scalar(select(AppUserProfile.timezone).where(AppUserProfile.app_user_id == user.id)) or 'UTC'
    return AnalyticsCapabilities(write_enabled=analytics_enabled(), state=current['state'],
                                 data_revision=current['data_revision'], timezone=zone)


@router.get('/exercises/{exercise_id}', response_model=ExerciseAnalytics | AnalyticsUnavailable | AnalyticsCompactV1)
async def exercise(exercise_id: PositiveID, params: Annotated[PeriodQuery, Query()], user: User, db: DB):
    result = await _read(db, user.id, lambda: service.exercise_analytics(db, user.id, exercise_id, params.to_query()))
    return pack_resource(result, 'exercise', user.firebase_uid) if params.encoding else result


@router.get('/muscles', response_model=MuscleAnalytics | AnalyticsUnavailable | AnalyticsCompactV1)
async def muscles(params: Annotated[PeriodQuery, Query()], user: User, db: DB):
    result = await _read(db, user.id, lambda: service.muscle_analytics(db, user.id, params.to_query()))
    return pack_resource(result, 'muscles', user.firebase_uid) if params.encoding else result


# Static route must precede the integer block route: validation does not fall through.
@router.get('/blocks/compare', response_model=BlockComparison | AnalyticsUnavailable | AnalyticsCompactV1)
async def compare(params: Annotated[CompareQuery, Query()], user: User, db: DB):
    result = await _read(db, user.id, lambda: service.compare_blocks(db, user.id, params.block_a_id, params.block_b_id, params.gym_filter))
    return pack_resource(result, 'comparison', user.firebase_uid) if params.encoding else result


@router.get('/blocks/{block_id}', response_model=BlockAnalytics | AnalyticsUnavailable | AnalyticsCompactV1)
async def block(block_id: PositiveID, params: Annotated[GymQuery, Query()], user: User, db: DB):
    result = await _read(db, user.id, lambda: service.block_analytics(db, user.id, block_id, params.gym_filter))
    return pack_resource(result, 'block', user.firebase_uid) if params.encoding else result
