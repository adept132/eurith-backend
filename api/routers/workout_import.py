from __future__ import annotations

import json
import logging
import re
from io import BytesIO
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from api.config import WorkoutImportVisionSettings
from api.deps import get_db
from api.schemas.workout_import import WorkoutImportMlFallbackResponse, WorkoutImportMlRequest
from api.services.app_user_service import get_current_app_user
from api.services.models import AppUser
from api.services.workout_import.openai_compatible import OpenAICompatibleWorkoutVisionProvider
from api.services.workout_import.provider import WorkoutVisionProvider
from api.services.workout_import.service import (
    DuplicateMlRequestError,
    MlRateLimitError,
    reject_ml_request,
    reserve_ml_request,
    run_ml_fallback,
)
from api.services.workout_import.types import ProviderErrorCode, VisionImage, WorkoutVisionProviderError


router = APIRouter(prefix="/workout-import", tags=["workout-import"])
logger = logging.getLogger("workout_import_ml")
MAX_TOTAL_BYTES = 8 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


async def get_workout_vision_provider() -> AsyncIterator[WorkoutVisionProvider | None]:
    try:
        settings = WorkoutImportVisionSettings.from_env()
    except ValueError:
        settings = None
    if settings is None:
        yield None
        return
    provider = OpenAICompatibleWorkoutVisionProvider(settings)
    try:
        yield provider
    finally:
        await provider.aclose()


def _error(code: str, message: str, retryable: bool) -> dict:
    return {"code": code, "message": message, "retryable": retryable}


def _valid_magic(content_type: str, body: bytes) -> bool:
    if content_type == "image/png":
        return body.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return body.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP"
    return False


def _normalize_image(content_type: str, body: bytes, *, max_output_bytes: int) -> bytes:
    expected_format = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[content_type]
    try:
        with Image.open(BytesIO(body)) as source:
            if source.format != expected_format or source.width <= 0 or source.height <= 0:
                raise ValueError("image format mismatch")
            if source.width > 12_000 or source.height > 12_000 or source.width * source.height > 20_000_000:
                raise ValueError("image dimensions exceed limits")
            source.load()
            oriented = ImageOps.exif_transpose(source)
            normalized = oriented.convert("RGBA" if oriented.mode in ("RGBA", "LA") and expected_format != "JPEG" else "RGB")
            output = BytesIO()
            normalized.save(output, format=expected_format)
            result = output.getvalue()
            if len(result) > max_output_bytes:
                raise HTTPException(
                    413,
                    detail=_error("payload_too_large", "Нормализованные изображения превышают 8 МиБ", False),
                )
            return result
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(415, detail=_error("invalid_image", "Изображение не удалось безопасно декодировать", False)) from exc


async def _read_raw_images(uploads: list[UploadFile]) -> tuple[tuple[str, bytes], ...]:
    if not 1 <= len(uploads) <= 3:
        raise HTTPException(413, detail=_error("too_many_images", "Разрешено от 1 до 3 изображений", False))
    images: list[tuple[str, bytes]] = []
    total = 0
    try:
        for upload in uploads:
            content_type = (upload.content_type or "").lower()
            if content_type not in ALLOWED_TYPES:
                raise HTTPException(415, detail=_error("unsupported_type", "Поддерживаются JPEG, PNG и WebP", False))
            remaining = MAX_TOTAL_BYTES - total
            body = await upload.read(remaining + 1)
            total += len(body)
            if total > MAX_TOTAL_BYTES:
                raise HTTPException(413, detail=_error("payload_too_large", "Изображения превышают 8 МиБ", False))
            if not body or not _valid_magic(content_type, body):
                raise HTTPException(415, detail=_error("invalid_image", "Содержимое не соответствует типу изображения", False))
            images.append((content_type, body))
    finally:
        for upload in uploads:
            await upload.close()
    return tuple(images)


async def _normalize_images(raw_images: tuple[tuple[str, bytes], ...]) -> tuple[VisionImage, ...]:
    images: list[VisionImage] = []
    total = 0
    for content_type, body in raw_images:
        normalized = await run_in_threadpool(
            _normalize_image,
            content_type,
            body,
            max_output_bytes=MAX_TOTAL_BYTES - total,
        )
        total += len(normalized)
        images.append(VisionImage(content_type=content_type, body=normalized))
    return tuple(images)


@router.post("/ml-fallback", response_model=WorkoutImportMlFallbackResponse)
async def workout_import_ml_fallback(
    response: Response,
    draft: str = Form(...),
    consent: str = Form(...),
    images: list[UploadFile] = File(...),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    app_user: AppUser = Depends(get_current_app_user),
    db: AsyncSession = Depends(get_db),
    provider: WorkoutVisionProvider | None = Depends(get_workout_vision_provider),
) -> WorkoutImportMlFallbackResponse:
    if consent != "one_time":
        raise HTTPException(400, detail=_error("consent_required", "Нужно согласие именно на этот запрос", False))
    if not IDEMPOTENCY_PATTERN.fullmatch(idempotency_key):
        raise HTTPException(400, detail=_error("invalid_idempotency_key", "Некорректный Idempotency-Key", False))
    if provider is None:
        raise HTTPException(503, detail=_error("unavailable", "Облачное распознавание сейчас выключено", True))
    try:
        parsed_draft = WorkoutImportMlRequest.model_validate(json.loads(draft))
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise HTTPException(422, detail=_error("invalid_draft", "Локальный черновик не прошёл проверку", False)) from exc
    app_user_id = app_user.id
    raw_images = await _read_raw_images(images)
    raw_byte_count = sum(len(body) for _, body in raw_images)
    try:
        record = await reserve_ml_request(
            db,
            app_user_id=app_user_id,
            idempotency_key=idempotency_key,
            image_count=len(raw_images),
            byte_count=raw_byte_count,
        )
    except DuplicateMlRequestError as exc:
        raise HTTPException(409, detail=_error("duplicate_request", "Этот запрос уже был обработан", False)) from exc
    except MlRateLimitError as exc:
        raise HTTPException(429, detail=_error("rate_limited", "Повторите позже", True), headers={"Retry-After": str(exc.retry_after)}) from exc
    try:
        bounded_images = await _normalize_images(raw_images)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        status = str(detail.get("code", "invalid_image"))
        if status not in {"invalid_image", "payload_too_large"}:
            status = "invalid_image"
        await reject_ml_request(db, record, status=status)
        raise
    byte_count = sum(len(image.body) for image in bounded_images)
    try:
        result = await run_ml_fallback(
            db,
            app_user_id=app_user_id,
            record=record,
            draft=parsed_draft,
            images=bounded_images,
            provider=provider,
        )
    except DuplicateMlRequestError as exc:
        raise HTTPException(409, detail=_error("duplicate_request", "Этот запрос уже был обработан", False)) from exc
    except MlRateLimitError as exc:
        response.headers["Retry-After"] = str(exc.retry_after)
        raise HTTPException(429, detail=_error("rate_limited", "Повторите позже", True), headers={"Retry-After": str(exc.retry_after)}) from exc
    except WorkoutVisionProviderError as exc:
        status_codes = {
            ProviderErrorCode.RATE_LIMITED: 429,
            ProviderErrorCode.TIMEOUT: 504,
            ProviderErrorCode.UNAVAILABLE: 503,
            ProviderErrorCode.INVALID_RESPONSE: 502,
        }
        headers = {"Retry-After": "60"} if exc.code == ProviderErrorCode.RATE_LIMITED else None
        raise HTTPException(
            status_codes[exc.code],
            detail=_error(exc.code.value, "Не удалось получить безопасное предложение", exc.retryable),
            headers=headers,
        ) from exc
    logger.info(
        "request_id=%s user_id=%s images=%s bytes=%s status=completed",
        result.request_id,
        app_user_id,
        len(bounded_images),
        byte_count,
    )
    return result
