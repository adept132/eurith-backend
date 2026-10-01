"""Authenticated progress-photo HTTP endpoints."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.body_photos import BodyPhotoPage, BodyPhotoRead
from api.services.app_user_service import get_current_app_user
from api.services.body_photo_images import MAX_UPLOAD_BYTES, normalize_photo
from api.services.body_photos import cleanup_pending_photos, owned_photo, photo_store
from api.services.models import AppUser, BodyProgressPhoto

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/photos", tags=["body-progress-photos"])


@router.post("", response_model=BodyPhotoRead, status_code=201)
async def upload_body_photo(
    response: Response,
    taken_on: date = Form(...),
    angle: Literal["front", "side", "back", "unspecified"] = Form("unspecified"),
    client_uuid: str = Form(..., min_length=1, max_length=64),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    user_id = current_user.id
    store = photo_store()
    if taken_on > date.today() + timedelta(days=1):
        raise HTTPException(400, "Дата фото не может быть в будущем")

    existing = (await db.execute(
        select(BodyProgressPhoto).where(
            BodyProgressPhoto.app_user_id == user_id,
            BodyProgressPhoto.client_uuid == client_uuid,
        )
    )).scalar_one_or_none()
    if existing is not None:
        if existing.state != "active":
            raise HTTPException(409, "Фото удаляется")
        response.status_code = 200
        return BodyPhotoRead.model_validate(existing)

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Фото больше 10 МБ")
    try:
        normalized = await run_in_threadpool(normalize_photo, raw, file.content_type or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    photo_id = uuid.uuid4()
    base = f"{user_id}/{photo_id.hex}"
    full_key, thumb_key = f"{base}/full.jpg", f"{base}/thumb.jpg"
    try:
        await run_in_threadpool(store.put, full_key, normalized.full)
        await run_in_threadpool(store.put, thumb_key, normalized.thumbnail)
    except Exception:
        await run_in_threadpool(store.delete, full_key)
        await run_in_threadpool(store.delete, thumb_key)
        raise HTTPException(503, "Не удалось сохранить фото")

    row = BodyProgressPhoto(
        id=photo_id, app_user_id=user_id, taken_on=taken_on, angle=angle,
        storage_key=full_key, thumbnail_key=thumb_key, mime_type="image/jpeg",
        byte_size=len(normalized.full), width=normalized.width, height=normalized.height,
        client_uuid=client_uuid, state="active",
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        await run_in_threadpool(store.delete, full_key)
        await run_in_threadpool(store.delete, thumb_key)
        winner = (await db.execute(
            select(BodyProgressPhoto).where(
                BodyProgressPhoto.app_user_id == user_id,
                BodyProgressPhoto.client_uuid == client_uuid,
                BodyProgressPhoto.state == "active",
            )
        )).scalar_one_or_none()
        if winner is None:
            raise HTTPException(409, "Повторная загрузка конфликтует с существующей")
        response.status_code = 200
        return BodyPhotoRead.model_validate(winner)
    except Exception:
        await db.rollback()
        await run_in_threadpool(store.delete, full_key)
        await run_in_threadpool(store.delete, thumb_key)
        raise
    await db.refresh(row)
    return BodyPhotoRead.model_validate(row)


def _encode_cursor(row: BodyProgressPhoto) -> str:
    raw = json.dumps([row.taken_on.isoformat(), row.created_at.isoformat(), str(row.id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(raw: str) -> tuple[date, datetime, uuid.UUID]:
    try:
        if len(raw) > 300:
            raise ValueError
        values = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if not isinstance(values, list) or len(values) != 3:
            raise ValueError
        return date.fromisoformat(values[0]), datetime.fromisoformat(values[1]), uuid.UUID(values[2])
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(422, "Некорректный курсор") from exc


@router.get("", response_model=BodyPhotoPage)
async def list_body_photos(
    limit: int = 30,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    photo_store()
    if not 1 <= limit <= 100:
        raise HTTPException(422, "limit должен быть от 1 до 100")
    stmt = select(BodyProgressPhoto).where(
        BodyProgressPhoto.app_user_id == current_user.id,
        BodyProgressPhoto.state == "active",
    )
    if cursor:
        cursor_date, cursor_created, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(or_(
            BodyProgressPhoto.taken_on < cursor_date,
            and_(BodyProgressPhoto.taken_on == cursor_date, BodyProgressPhoto.created_at < cursor_created),
            and_(BodyProgressPhoto.taken_on == cursor_date, BodyProgressPhoto.created_at == cursor_created, BodyProgressPhoto.id < cursor_id),
        ))
    rows = (await db.execute(
        stmt.order_by(
            BodyProgressPhoto.taken_on.desc(),
            BodyProgressPhoto.created_at.desc(),
            BodyProgressPhoto.id.desc(),
        ).limit(limit + 1)
    )).scalars().all()
    items = rows[:limit]
    return BodyPhotoPage(
        items=[BodyPhotoRead.model_validate(row) for row in items],
        next_cursor=_encode_cursor(items[-1]) if len(rows) > limit else None,
    )

@router.get("/{photo_id}/content")
async def body_photo_content(
    photo_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    row = await owned_photo(db, current_user.id, photo_id)
    try:
        content = await run_in_threadpool(photo_store().get, row.storage_key)
    except FileNotFoundError:
        raise HTTPException(404, "Фото не найдено")
    return Response(content, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@router.get("/{photo_id}/thumbnail")
async def body_photo_thumbnail(
    photo_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    row = await owned_photo(db, current_user.id, photo_id)
    try:
        content = await run_in_threadpool(photo_store().get, row.thumbnail_key)
    except FileNotFoundError:
        raise HTTPException(404, "Фото не найдено")
    return Response(content, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@router.delete("/{photo_id}", status_code=204)
async def delete_body_photo(
    photo_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    row = await owned_photo(db, current_user.id, photo_id, include_deleting=True)
    row.state = "deleting"
    await db.commit()
    await cleanup_pending_photos(db, current_user.id)
    return Response(status_code=204)
