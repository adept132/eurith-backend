from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
import uuid

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.schemas.workout_import import (
    MlWorkoutImportResult,
    WorkoutImportMlFallbackResponse,
    WorkoutImportMlRequest,
)
from api.services.exercise_matcher import ExerciseMatcher
from api.services.models import WorkoutImportMlRequestRecord
from api.services.workout_import.provider import WorkoutVisionProvider
from api.services.workout_import.types import (
    ProviderErrorCode,
    VisionImage,
    WorkoutVisionProviderError,
    WorkoutVisionRequest,
)


RATE_LIMIT = 5
RATE_WINDOW = timedelta(minutes=10)


class DuplicateMlRequestError(Exception):
    pass


class MlRateLimitError(Exception):
    retry_after = 600


async def _reserve(
    db: AsyncSession,
    *,
    app_user_id: int,
    idempotency_key: str,
    image_count: int,
    byte_count: int,
) -> WorkoutImportMlRequestRecord:
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": app_user_id})
    duplicate = await db.scalar(
        select(WorkoutImportMlRequestRecord.id).where(
            WorkoutImportMlRequestRecord.app_user_id == app_user_id,
            WorkoutImportMlRequestRecord.idempotency_key == idempotency_key,
        )
    )
    if duplicate is not None:
        await db.rollback()
        raise DuplicateMlRequestError

    cutoff = datetime.now(timezone.utc) - RATE_WINDOW
    attempts = await db.scalar(
        select(func.count()).select_from(WorkoutImportMlRequestRecord).where(
            WorkoutImportMlRequestRecord.app_user_id == app_user_id,
            WorkoutImportMlRequestRecord.created_at >= cutoff,
        )
    )
    if int(attempts or 0) >= RATE_LIMIT:
        await db.rollback()
        raise MlRateLimitError

    record = WorkoutImportMlRequestRecord(
        app_user_id=app_user_id,
        request_id=str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        image_count=image_count,
        byte_count=byte_count,
        status="reserved",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def _guard_result(
    db: AsyncSession,
    *,
    app_user_id: int,
    draft: WorkoutImportMlRequest,
    result: MlWorkoutImportResult,
    image_count: int,
) -> MlWorkoutImportResult:
    allowed = set(draft.unresolved_source_ids)
    accepted = []
    warnings = list(result.warnings)
    truth_flags: dict[str, set[str]] = {}

    for patch in result.suggestions:
        if patch.source_id not in allowed:
            warnings.append(f"ignored_resolved_source:{patch.source_id}")
            continue
        if any(item.image_index >= image_count for item in patch.evidence):
            warnings.append(f"invalid_image_evidence:{patch.source_id}")
            continue
        if patch.field in ("bodyweight", "assisted") and patch.value is True:
            truth_flags.setdefault(patch.source_id, set()).add(patch.field)
        if patch.field == "exercise_name":
            best, _ = await ExerciseMatcher.find_or_create_exercise(
                db, app_user_id, str(patch.value), min_similarity=0.8
            )
            if best is None or best.get("similarity", 0) < 0.95:
                warnings.append(f"exercise_name_requires_review:{patch.source_id}")
                continue
        accepted.append(patch)

    conflicting = {
        source_id for source_id, flags in truth_flags.items() if flags == {"bodyweight", "assisted"}
    }
    if conflicting:
        accepted = [
            patch
            for patch in accepted
            if not (patch.source_id in conflicting and patch.field in ("bodyweight", "assisted"))
        ]
        warnings.extend(f"conflicting_set_mode:{source_id}" for source_id in sorted(conflicting))
    return MlWorkoutImportResult(suggestions=accepted, warnings=warnings)


async def run_ml_fallback(
    db: AsyncSession,
    *,
    app_user_id: int,
    idempotency_key: str,
    draft: WorkoutImportMlRequest,
    images: tuple[VisionImage, ...],
    provider: WorkoutVisionProvider,
) -> WorkoutImportMlFallbackResponse:
    record = await _reserve(
        db,
        app_user_id=app_user_id,
        idempotency_key=idempotency_key,
        image_count=len(images),
        byte_count=sum(len(image.body) for image in images),
    )
    started = time.monotonic()
    try:
        raw_result = await provider.extract(
            WorkoutVisionRequest(request_id=record.request_id, draft=draft, images=images)
        )
        result = MlWorkoutImportResult.model_validate(raw_result)
        result = await _guard_result(
            db,
            app_user_id=app_user_id,
            draft=draft,
            result=result,
            image_count=len(images),
        )
        record.status = "completed"
    except WorkoutVisionProviderError as exc:
        record.status = exc.code.value
        record.latency_ms = max(0, int((time.monotonic() - started) * 1000))
        await db.commit()
        raise
    except (ValidationError, ValueError, TypeError) as exc:
        record.status = ProviderErrorCode.INVALID_RESPONSE.value
        record.latency_ms = max(0, int((time.monotonic() - started) * 1000))
        await db.commit()
        raise WorkoutVisionProviderError(
            ProviderErrorCode.INVALID_RESPONSE, retryable=False
        ) from exc

    record.latency_ms = max(0, int((time.monotonic() - started) * 1000))
    await db.commit()
    return WorkoutImportMlFallbackResponse(
        request_id=record.request_id,
        draft_id=f"ml-draft-{record.request_id}",
        suggestions=result.suggestions,
        warnings=result.warnings,
    )
