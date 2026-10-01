"""Композиция тела: запись замеров, обзор с историей/ИМТ, история записей."""

from datetime import date, timedelta
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.routers.body_photos import router as body_photos_router
from api.schemas.body import BodyEntry, BodyEntryRequest, BodyOverviewResponse
from api.services.app_user_service import get_current_app_user
from api.services.body_service import (
    BODY_METRIC_KEYS,
    get_body_entries,
    get_body_overview,
    record_body_entry,
)
from api.services.body_comparison import body_comparison
from api.services.body_photos import owned_photo
from api.services.models import AppUser, AppUserProfile

router = APIRouter(prefix="/body", tags=["body-composition"])


async def _gender(db: AsyncSession, app_user_id: int) -> Optional[str]:
    profile = (await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    return profile.gender if profile else None


@router.post("/entry", response_model=BodyOverviewResponse)
async def create_body_entry(
    payload: BodyEntryRequest,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    """Записать снимок композиции (вес/рост/%жира и/или обхваты).

    Если % жира не задан, но пришли обхваты — считаем его по US Navy."""
    if payload.measurements:
        bad = [k for k in payload.measurements if k not in BODY_METRIC_KEYS]
        if bad:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Неизвестные метрики: {', '.join(bad)}",
            )

    nothing = (
        payload.weight is None
        and payload.height is None
        and payload.body_fat is None
        and not payload.measurements
    )
    if nothing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нет данных для записи")
    if payload.measured_on and payload.measured_on > date.today() + timedelta(days=1):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Дата замера не может быть в будущем")

    await record_body_entry(
        db,
        current_user.id,
        weight=payload.weight,
        height=payload.height,
        body_fat=payload.body_fat,
        measurements=payload.measurements,
        gender=await _gender(db, current_user.id),
        client_uuid=payload.client_uuid,
        measured_on=payload.measured_on,
    )
    return BodyOverviewResponse(**await get_body_overview(db, current_user.id))


@router.get("/overview", response_model=BodyOverviewResponse)
async def body_overview(
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    return BodyOverviewResponse(**await get_body_overview(db, current_user.id))


@router.get("/entries", response_model=List[BodyEntry])
async def body_entries(
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    """История записей — снимки по датам (свежие сверху)."""
    return [BodyEntry(**e) for e in await get_body_entries(db, current_user.id)]


@router.get("/comparison")
async def compare_body_photos(
    before_photo_id: UUID,
    after_photo_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    before = await owned_photo(db, current_user.id, before_photo_id)
    after = await owned_photo(db, current_user.id, after_photo_id)
    if before.taken_on >= after.taken_on:
        raise HTTPException(400, "Первое фото должно быть раньше второго")
    return await body_comparison(db, current_user.id, before, after)

router.include_router(body_photos_router)
