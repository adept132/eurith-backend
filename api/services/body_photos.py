"""Owner-scoped photo queries and private volume configuration."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID
import logging

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.body_photo_store import FilePhotoStore
from api.services.models import BodyProgressPhoto

logger = logging.getLogger(__name__)


def photo_store() -> FilePhotoStore:
    raw = os.getenv("BODY_PHOTO_ROOT")
    if not raw:
        raise HTTPException(503, "Хранилище фото не настроено")
    if not Path(raw).is_absolute():
        raise HTTPException(503, "Приватное хранилище фото недоступно")
    root = Path(raw).resolve()
    project_root = Path(__file__).resolve().parents[2]
    if not root.is_dir() or root.is_relative_to(project_root):
        raise HTTPException(503, "Приватное хранилище фото недоступно")
    if not os.access(root, os.R_OK | os.W_OK):
        raise HTTPException(503, "Приватное хранилище фото недоступно")
    return FilePhotoStore(root)


async def owned_photo(
    db: AsyncSession, user_id: int, photo_id: UUID, *, include_deleting: bool = False
) -> BodyProgressPhoto:
    stmt = select(BodyProgressPhoto).where(
        BodyProgressPhoto.id == photo_id,
        BodyProgressPhoto.app_user_id == user_id,
    )
    if not include_deleting:
        stmt = stmt.where(BodyProgressPhoto.state == "active")
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Фото не найдено")
    return row


async def cleanup_pending_photos(db: AsyncSession, user_id: int | None = None) -> int:
    """Retry file removal for hidden photos; leave failed rows for the next run."""
    stmt = select(BodyProgressPhoto).where(BodyProgressPhoto.state == "deleting")
    if user_id is not None:
        stmt = stmt.where(BodyProgressPhoto.app_user_id == user_id)
    rows = (await db.execute(stmt.order_by(BodyProgressPhoto.created_at))).scalars().all()
    if not rows:
        return 0
    store = photo_store()
    removed = 0
    for row in rows:
        try:
            await run_in_threadpool(store.delete, row.storage_key)
            await run_in_threadpool(store.delete, row.thumbnail_key)
        except Exception:
            logger.exception("Progress photo cleanup failed for id=%s", row.id)
            continue
        await db.delete(row)
        await db.commit()
        removed += 1
    return removed
