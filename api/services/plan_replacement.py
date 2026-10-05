from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import exists, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.schemas.plan_replacement import (
    ComparedExercise,
    PlanReplacementPreview,
    PlanReplacementResult,
    PlanReplacementSourceKind,
)
from api.services.models import (
    Exercise,
    AppUserProfile,
    PlanReplacementOperation,
    UserCalendarDay,
    UserSplit,
    WorkoutPlan,
    WorkoutPlanExercise,
    WorkoutRoutine,
    WorkoutRoutineExercise,
    WorkoutSession,
    WorkoutSessionExercise,
)
from api.services.workout_structure import (
    WorkoutStructureDraft,
    WorkoutStructureExerciseDraft,
    extract_workout_structure,
)
from api.services.validator import AntiSuicideValidator, PlanExerciseInput


class PlanReplacementError(Exception):
    pass


class ReplacementSourceNotFound(PlanReplacementError):
    pass


class TargetPlanNotFound(PlanReplacementError):
    pass


class EmptyReplacementStructure(PlanReplacementError):
    pass


class NoReplaceablePlanDates(PlanReplacementError):
    pass


class InvalidReplacementExercise(PlanReplacementError):
    def __init__(self, exercise_ids: list[int]):
        self.exercise_ids = exercise_ids


class StaleReplacementPreview(PlanReplacementError):
    def __init__(self, preview: PlanReplacementPreview):
        self.preview = preview


class PlanReplacementIdempotencyConflict(PlanReplacementError):
    pass


def _apply_request_fingerprint(
    source_kind: PlanReplacementSourceKind,
    source_id: int,
    plan_id: int,
    preview_token: str,
) -> str:
    payload = json.dumps(
        {
            "source_kind": source_kind,
            "source_id": source_id,
            "target_plan_id": plan_id,
            "preview_token": preview_token,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _idempotent_result(
    db: AsyncSession,
    user_id: int,
    idempotency_key: str,
    request_fingerprint: str,
) -> PlanReplacementResult | None:
    operation = (
        await db.execute(
            select(PlanReplacementOperation).where(
                PlanReplacementOperation.app_user_id == user_id,
                PlanReplacementOperation.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if operation is None:
        return None
    if operation.request_fingerprint != request_fingerprint:
        raise PlanReplacementIdempotencyConflict
    return PlanReplacementResult(
        new_plan_id=operation.new_plan_id,
        affected_dates=[date.fromisoformat(value) for value in operation.affected_dates],
    )


def _parse_rep_range(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    parts = [part.strip() for part in value.split("-", 1)]
    try:
        rep_min = int(parts[0])
        rep_max = int(parts[-1])
    except ValueError:
        return None, None
    return rep_min, rep_max


def _indexed_by_exercise(items: list[Any]) -> list[tuple[tuple[int, int], Any]]:
    occurrences: defaultdict[int, int] = defaultdict(int)
    result: list[tuple[tuple[int, int], Any]] = []
    for item in sorted(items, key=lambda value: value.order_index):
        exercise_id = int(item.exercise_id)
        key = (exercise_id, occurrences[exercise_id])
        occurrences[exercise_id] += 1
        result.append((key, item))
    return result


def _compared(
    old: Any | None,
    new: WorkoutStructureExerciseDraft | None,
    exercise_names: dict[int, str],
) -> ComparedExercise:
    exercise_id = int(new.exercise_id if new is not None else old.exercise_id)
    old_rep_min, old_rep_max = _parse_rep_range(
        old.override_reps if old is not None else None
    )
    old_values = {
        "order_index": int(old.order_index) if old is not None else None,
        "target_sets": int(old.target_sets) if old is not None else None,
        "rep_min": old_rep_min,
        "rep_max": old_rep_max,
        "target_rir": old.override_rir if old is not None else None,
        "superset_group": (
            str(old.superset_group_id) if old is not None and old.superset_group_id else None
        ),
    }
    new_values = {
        "order_index": int(new.order_index) if new is not None else None,
        "target_sets": int(new.target_sets) if new is not None else None,
        "rep_min": new.rep_min if new is not None else None,
        "rep_max": new.rep_max if new is not None else None,
        "target_rir": new.target_rir if new is not None else None,
        "superset_group": new.superset_group if new is not None else None,
    }
    return ComparedExercise(
        exercise_id=exercise_id,
        exercise_name=exercise_names.get(exercise_id, f"Exercise {exercise_id}"),
        old_order_index=old_values["order_index"],
        new_order_index=new_values["order_index"],
        old_target_sets=old_values["target_sets"],
        new_target_sets=new_values["target_sets"],
        old_rep_min=old_values["rep_min"],
        new_rep_min=new_values["rep_min"],
        old_rep_max=old_values["rep_max"],
        new_rep_max=new_values["rep_max"],
        old_target_rir=old_values["target_rir"],
        new_target_rir=new_values["target_rir"],
        changed_fields=[
            field for field in old_values if old_values[field] != new_values[field]
        ],
    )


def _preview_token(
    *,
    source_kind: PlanReplacementSourceKind,
    source_id: int,
    source_structure: WorkoutStructureDraft,
    target_plan_id: int,
    target_plan_revision: int,
    target_plan_structure: list[dict[str, Any]],
    affected_calendar_day_ids: list[int],
) -> str:
    payload = {
        "affected_calendar_day_ids": sorted(affected_calendar_day_ids),
        "source": {
            "id": source_id,
            "kind": source_kind,
            "structure": source_structure.model_dump(mode="json"),
        },
        "target_plan": {
            "id": target_plan_id,
            "revision": target_plan_revision,
            "structure": target_plan_structure,
        },
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _target_structure(plan: WorkoutPlan) -> list[dict[str, Any]]:
    return [
        {
            "exercise_id": int(item.exercise_id),
            "order_index": int(item.order_index),
            "superset_group": str(item.superset_group_id) if item.superset_group_id else None,
            "target_sets": int(item.target_sets),
            "override_reps": item.override_reps,
            "override_rir": item.override_rir,
        }
        for item in sorted(plan.exercises, key=lambda value: value.order_index)
    ]


def build_replacement_preview(
    *,
    source_kind: PlanReplacementSourceKind,
    source_id: int,
    source_structure: WorkoutStructureDraft,
    target_plan: WorkoutPlan,
    affected_days: list[UserCalendarDay],
    exercise_names: dict[int, str],
) -> PlanReplacementPreview:
    ordered_days = sorted(affected_days, key=lambda day: (day.target_date, day.id))
    if not ordered_days:
        raise NoReplaceablePlanDates

    old_indexed = dict(_indexed_by_exercise(list(target_plan.exercises)))
    new_indexed = dict(_indexed_by_exercise(list(source_structure.exercises)))
    old_keys = set(old_indexed)
    new_keys = set(new_indexed)

    added = [
        _compared(None, new_indexed[key], exercise_names)
        for key in sorted(new_keys - old_keys, key=lambda item: new_indexed[item].order_index)
    ]
    removed = [
        _compared(old_indexed[key], None, exercise_names)
        for key in sorted(old_keys - new_keys, key=lambda item: old_indexed[item].order_index)
    ]
    modified = []
    for key in sorted(old_keys & new_keys, key=lambda item: new_indexed[item].order_index):
        comparison = _compared(old_indexed[key], new_indexed[key], exercise_names)
        if comparison.changed_fields:
            modified.append(comparison)

    return PlanReplacementPreview(
        target_plan_id=int(target_plan.id),
        source_kind=source_kind,
        source_id=source_id,
        effective_from=ordered_days[0].target_date,
        affected_dates=[day.target_date for day in ordered_days],
        old_total_sets=sum(int(item.target_sets) for item in target_plan.exercises),
        new_total_sets=sum(item.target_sets for item in source_structure.exercises),
        added=added,
        removed=removed,
        modified=modified,
        preview_token=_preview_token(
            source_kind=source_kind,
            source_id=source_id,
            source_structure=source_structure,
            target_plan_id=int(target_plan.id),
            target_plan_revision=int(target_plan.revision),
            target_plan_structure=_target_structure(target_plan),
            affected_calendar_day_ids=[int(day.id) for day in ordered_days],
        ),
    )


async def _load_source_structure(
    db: AsyncSession,
    user_id: int,
    source_kind: PlanReplacementSourceKind,
    source_id: int,
    *,
    for_update: bool = False,
) -> WorkoutStructureDraft:
    if source_kind == "workout":
        query = (
            select(WorkoutSession)
            .where(
                WorkoutSession.id == source_id,
                WorkoutSession.app_user_id == user_id,
            )
            .options(
                selectinload(WorkoutSession.exercises).selectinload(
                    WorkoutSessionExercise.sets
                )
            )
        )
        if for_update:
            query = query.with_for_update()
        workout = (
            await db.execute(query)
        ).scalar_one_or_none()
        if workout is None:
            raise ReplacementSourceNotFound
        structure = extract_workout_structure(workout)
    else:
        query = (
            select(WorkoutRoutine)
            .where(
                WorkoutRoutine.id == source_id,
                WorkoutRoutine.app_user_id == user_id,
            )
            .options(selectinload(WorkoutRoutine.exercises))
        )
        if for_update:
            query = query.with_for_update()
        routine = (
            await db.execute(query)
        ).scalar_one_or_none()
        if routine is None:
            raise ReplacementSourceNotFound
        structure = WorkoutStructureDraft(
            name=routine.name,
            notes=routine.notes,
            exercises=[
                WorkoutStructureExerciseDraft(
                    exercise_id=item.exercise_id,
                    order_index=item.order_index,
                    superset_group=item.superset_group,
                    target_sets=item.target_sets,
                    set_kinds=item.set_kinds,
                    rep_min=item.rep_min,
                    rep_max=item.rep_max,
                    target_rir=item.target_rir,
                    rest_seconds=item.rest_seconds,
                    notes=item.notes,
                )
                for item in routine.exercises
            ],
        )
    if not structure.exercises or any(item.target_sets <= 0 for item in structure.exercises):
        raise EmptyReplacementStructure
    return structure


async def _load_target_plan(
    db: AsyncSession, user_id: int, plan_id: int, *, for_update: bool = False
) -> WorkoutPlan:
    query = (
        select(WorkoutPlan)
        .where(
            WorkoutPlan.id == plan_id,
            WorkoutPlan.app_user_id == user_id,
            WorkoutPlan.is_archived.is_(False),
        )
        .options(selectinload(WorkoutPlan.exercises))
    )
    if for_update:
        query = query.with_for_update()
    plan = (await db.execute(query)).scalar_one_or_none()
    if plan is None:
        raise TargetPlanNotFound
    return plan


async def _load_affected_days(
    db: AsyncSession,
    user_id: int,
    plan_id: int,
    today: date,
    *,
    for_update: bool = False,
) -> list[UserCalendarDay]:
    active_session = exists(
        select(WorkoutSession.id).where(
            WorkoutSession.calendar_day_id == UserCalendarDay.id,
            WorkoutSession.app_user_id == user_id,
            WorkoutSession.status.in_(("active", "paused")),
        )
    )
    query = (
        select(UserCalendarDay)
        .where(
            UserCalendarDay.app_user_id == user_id,
            UserCalendarDay.plan_id == plan_id,
            UserCalendarDay.target_date >= today,
            UserCalendarDay.status == "planned",
            UserCalendarDay.actual_workout_session_id.is_(None),
            UserCalendarDay.is_rest_day.is_(False),
            ~active_session,
        )
        .order_by(UserCalendarDay.target_date, UserCalendarDay.id)
    )
    if for_update:
        query = query.with_for_update()
    return list(
        (
            await db.execute(query)
        ).scalars().all()
    )


async def preview_replacement(
    db: AsyncSession,
    user_id: int,
    source: tuple[PlanReplacementSourceKind, int],
    plan_id: int,
    today: date,
) -> PlanReplacementPreview:
    source_kind, source_id = source
    structure = await _load_source_structure(db, user_id, source_kind, source_id)
    target_plan = await _load_target_plan(db, user_id, plan_id)
    affected_days = await _load_affected_days(db, user_id, plan_id, today)
    if not affected_days:
        raise NoReplaceablePlanDates
    exercise_ids = {
        *(item.exercise_id for item in structure.exercises),
        *(item.exercise_id for item in target_plan.exercises),
    }
    exercise_names = dict(
        (
            await db.execute(
                select(Exercise.id, Exercise.name).where(Exercise.id.in_(exercise_ids))
            )
        ).all()
    )
    return build_replacement_preview(
        source_kind=source_kind,
        source_id=source_id,
        source_structure=structure,
        target_plan=target_plan,
        affected_days=affected_days,
        exercise_names=exercise_names,
    )


def _override_reps(item: WorkoutStructureExerciseDraft) -> str | None:
    if item.rep_min is None and item.rep_max is None:
        return None
    if item.rep_min is None:
        return str(item.rep_max)
    if item.rep_max is None or item.rep_max == item.rep_min:
        return str(item.rep_min)
    return f"{item.rep_min}-{item.rep_max}"


def _superset_uuid(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"fitpilot:superset:{value}")


async def apply_replacement(
    db: AsyncSession,
    user_id: int,
    source: tuple[PlanReplacementSourceKind, int],
    plan_id: int,
    today: date,
    preview_token: str,
    idempotency_key: str,
) -> PlanReplacementResult:
    source_kind, source_id = source
    request_fingerprint = _apply_request_fingerprint(
        source_kind, source_id, plan_id, preview_token
    )
    # Serialize every use of one user-scoped key, even when conflicting
    # requests point at different source/target rows and share no row lock.
    await db.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(CAST(:lock_key AS text), CAST(:lock_seed AS bigint)))"
        ),
        {"lock_key": idempotency_key, "lock_seed": user_id},
    )
    replay = await _idempotent_result(
        db, user_id, idempotency_key, request_fingerprint
    )
    if replay is not None:
        return replay
    structure = await _load_source_structure(
        db, user_id, source_kind, source_id, for_update=True
    )
    try:
        target_plan = await _load_target_plan(db, user_id, plan_id, for_update=True)
    except TargetPlanNotFound:
        replay = await _idempotent_result(
            db, user_id, idempotency_key, request_fingerprint
        )
        if replay is not None:
            return replay
        raise
    # A concurrent request can miss the operation before waiting on the plan
    # row lock. Recheck after acquiring that lock before cloning anything.
    replay = await _idempotent_result(
        db, user_id, idempotency_key, request_fingerprint
    )
    if replay is not None:
        return replay
    affected_days = await _load_affected_days(
        db, user_id, plan_id, today, for_update=True
    )
    if not affected_days:
        raise NoReplaceablePlanDates

    source_ids = {item.exercise_id for item in structure.exercises}
    exercises = list(
        (
            await db.execute(
                select(Exercise).where(
                    Exercise.id.in_(source_ids),
                    or_(
                        Exercise.app_user_id.is_(None),
                        Exercise.app_user_id == user_id,
                    ),
                )
            )
        ).scalars().all()
    )
    exercises_by_id = {item.id: item for item in exercises}
    invalid = sorted(source_ids - set(exercises_by_id))
    if invalid:
        raise InvalidReplacementExercise(invalid)

    all_ids = source_ids | {item.exercise_id for item in target_plan.exercises}
    exercise_names = dict(
        (
            await db.execute(
                select(Exercise.id, Exercise.name).where(Exercise.id.in_(all_ids))
            )
        ).all()
    )
    current_preview = build_replacement_preview(
        source_kind=source_kind,
        source_id=source_id,
        source_structure=structure,
        target_plan=target_plan,
        affected_days=affected_days,
        exercise_names=exercise_names,
    )
    if current_preview.preview_token != preview_token:
        raise StaleReplacementPreview(current_preview)

    profile = (
        await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == user_id)
        )
    ).scalar_one_or_none()
    experience = profile.experience_level if profile else "beginner"
    validator_rows = []
    for item in structure.exercises:
        exercise = exercises_by_id[item.exercise_id]
        secondary = list(exercise.secondary_muscle_groups or [])
        validator_rows.append(
            PlanExerciseInput(
                exercise_id=item.exercise_id,
                fatigue_tier=exercise.fatigue_tier,
                primary_muscle=exercise.main_muscle_group,
                secondary_muscle=secondary[0] if secondary else None,
                target_sets=item.target_sets,
                superset_group_id=_superset_uuid(item.superset_group),
            )
        )
    AntiSuicideValidator.validate_workout_plan(experience, validator_rows)

    new_plan = WorkoutPlan(
        app_user_id=user_id,
        name=target_plan.name,
        day_tag=target_plan.day_tag,
        micro_tag=target_plan.micro_tag,
        meso_tag=target_plan.meso_tag,
        supersedes_plan_id=target_plan.id,
        is_archived=False,
        revision=target_plan.revision + 1,
    )
    db.add(new_plan)
    await db.flush()
    db.add_all(
        [
            WorkoutPlanExercise(
                plan_id=new_plan.id,
                exercise_id=item.exercise_id,
                order_index=item.order_index,
                superset_group_id=_superset_uuid(item.superset_group),
                target_sets=item.target_sets,
                override_reps=_override_reps(item),
                override_rir=item.target_rir,
            )
            for item in structure.exercises
        ]
    )
    selected_splits = list(
        (
            await db.execute(
                select(UserSplit)
                .where(UserSplit.app_user_id == user_id)
                .with_for_update()
            )
        ).scalars().all()
    )
    for split in selected_splits:
        selected = dict(split.selected_plans or {})
        changed = False
        for key, value in selected.items():
            if str(key).isdigit() and str(value) == str(target_plan.id):
                selected[key] = new_plan.id
                changed = True
        if changed:
            split.selected_plans = selected
    affected_ids = [item.id for item in affected_days]
    await db.execute(
        update(UserCalendarDay)
        .where(
            UserCalendarDay.id.in_(affected_ids),
            UserCalendarDay.app_user_id == user_id,
            UserCalendarDay.plan_id == target_plan.id,
        )
        .values(plan_id=new_plan.id)
    )

    remaining_calendar_reference = await db.scalar(
        select(UserCalendarDay.id)
        .where(
            UserCalendarDay.app_user_id == user_id,
            UserCalendarDay.plan_id == target_plan.id,
            UserCalendarDay.target_date >= today,
        )
        .limit(1)
    )
    active_session_reference = await db.scalar(
        select(WorkoutSession.id)
        .where(
            WorkoutSession.app_user_id == user_id,
            WorkoutSession.plan_id == target_plan.id,
            WorkoutSession.status.in_(("active", "paused")),
        )
        .limit(1)
    )
    if remaining_calendar_reference is None and active_session_reference is None:
        target_plan.is_archived = True

    db.add(
        PlanReplacementOperation(
            app_user_id=user_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            new_plan_id=new_plan.id,
            affected_dates=[value.isoformat() for value in current_preview.affected_dates],
        )
    )

    await db.commit()
    return PlanReplacementResult(
        new_plan_id=new_plan.id,
        affected_dates=current_preview.affected_dates,
    )
