from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.equipment import normalize_equipment_list
from api.services.muscle_keys import to_system_key
from api.services.readiness import params as readiness_params
from api.services.readiness.repository import (
    build_exercise_target,
    lock_user_safety_context,
)
from api.services.readiness.types import CheckinSignals
from api.services.readiness.verdict import build_verdict, level_for_exercise
from api.services.volume.landmarks import SESSION_MAX, landmarks_for, scale_landmarks
from api.services.volume.measure import apply_adjustments
from api.schemas.carry_over import (
    CarryOverDiscard,
    CarryOverPlacement as CarryOverPlacementSchema,
    CarryOverPreviewItem,
    CarryOverResponse,
)


MatchKind = Literal["exact", "compatible"]
DiscardReason = Literal[
    "session_cap",
    "mrv",
    "deload",
    "readiness",
    "pain",
    "equipment",
    "microcycle_end",
    "no_compatible_day",
]


def _effective_plan_sets(
    plan_sets: list[tuple[int, int]], adjustments: list[dict] | None
) -> list[int]:
    compiled = [
        {"exercise_id": exercise_id, "target_sets": target_sets}
        for exercise_id, target_sets in plan_sets
    ]
    return [
        int(item.get("target_sets") or 0)
        for item in apply_adjustments(compiled, adjustments)
    ]


def _is_serialization_failure(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == "40001" or getattr(
            current, "pgcode", None
        ) == "40001":
            return True
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return False


@dataclass(frozen=True)
class CarryOverCandidate:
    calendar_day_id: int
    target_date: date
    exercise_id: int
    target_exercise_name: str
    target_day_name: str
    muscle: str
    match_kind: MatchKind
    session_cap_remaining: int
    muscle_mrv_remaining: int
    is_deload: bool = False
    readiness_level: str = "ok"
    has_pain: bool = False
    equipment_available: bool = True
    within_microcycle: bool = True

    @property
    def hard_block_reason(self) -> DiscardReason | None:
        if not self.within_microcycle:
            return "microcycle_end"
        if self.is_deload:
            return "deload"
        if self.readiness_level != "ok":
            return "readiness"
        if self.has_pain:
            return "pain"
        if not self.equipment_available:
            return "equipment"
        return None


@dataclass(frozen=True)
class CarryOverPlacement:
    calendar_day_id: int
    target_date: date
    exercise_id: int
    target_exercise_name: str
    target_day_name: str
    delta_sets: int
    match_kind: MatchKind


@dataclass(frozen=True)
class CarryOverDistribution:
    requested_sets: int
    placements: list[CarryOverPlacement]
    discarded_reasons: list[DiscardReason]

    @property
    def placed_sets(self) -> int:
        return sum(item.delta_sets for item in self.placements)

    @property
    def discarded_sets(self) -> int:
        return len(self.discarded_reasons)


@dataclass(frozen=True)
class SourceCarryOverDistribution:
    source_exercise_id: int
    source_exercise_name: str
    exercise_id: int
    muscle: str
    distribution: CarryOverDistribution


@dataclass(frozen=True)
class CarryOverSourceRequest:
    source_exercise_id: int
    source_exercise_name: str
    exercise_id: int
    muscle: str
    requested_sets: int
    candidates: list[CarryOverCandidate]
    empty_reason: DiscardReason = "microcycle_end"


@dataclass(frozen=True)
class CarryOverPreview:
    source_session_id: int
    microcycle_end: date
    items: list[SourceCarryOverDistribution]


def _discard_reason(
    candidates: list[CarryOverCandidate],
    session_remaining: dict[int, int],
    mrv_remaining: dict[str, int],
) -> DiscardReason:
    in_cycle = [item for item in candidates if item.within_microcycle]
    if not in_cycle:
        return "microcycle_end"

    safe = [item for item in in_cycle if item.hard_block_reason is None]
    if safe:
        if all(mrv_remaining.get(item.muscle, 0) <= 0 for item in safe):
            return "mrv"
        if all(session_remaining.get(item.calendar_day_id, 0) <= 0 for item in safe):
            return "session_cap"

    for item in sorted(in_cycle, key=lambda row: (row.target_date, row.calendar_day_id)):
        if item.hard_block_reason is not None:
            return item.hard_block_reason
    return "no_compatible_day"


def distribute_carry_over(
    requested_sets: int,
    candidates: list[CarryOverCandidate],
    *,
    empty_reason: DiscardReason = "microcycle_end",
) -> CarryOverDistribution:
    """Distribute one set per pass, exhausting exact matches before compatible ones."""

    if requested_sets < 0:
        raise ValueError("requested_sets must be non-negative")

    ordered = sorted(candidates, key=lambda row: (row.target_date, row.calendar_day_id))
    session_remaining = {
        item.calendar_day_id: max(0, int(item.session_cap_remaining))
        for item in ordered
    }
    mrv_remaining: dict[str, int] = {}
    for item in ordered:
        remaining = max(0, int(item.muscle_mrv_remaining))
        mrv_remaining[item.muscle] = min(
            remaining,
            mrv_remaining.get(item.muscle, remaining),
        )

    allocated: dict[tuple[int, int, MatchKind], int] = {}
    remaining_sets = requested_sets

    for match_kind in ("exact", "compatible"):
        tier = [
            item
            for item in ordered
            if item.match_kind == match_kind and item.hard_block_reason is None
        ]
        while remaining_sets > 0:
            progressed = False
            for item in tier:
                if remaining_sets <= 0:
                    break
                if session_remaining[item.calendar_day_id] <= 0:
                    continue
                if mrv_remaining.get(item.muscle, 0) <= 0:
                    continue

                key = (item.calendar_day_id, item.exercise_id, item.match_kind)
                allocated[key] = allocated.get(key, 0) + 1
                session_remaining[item.calendar_day_id] -= 1
                mrv_remaining[item.muscle] -= 1
                remaining_sets -= 1
                progressed = True
            if not progressed:
                break

    by_key = {
        (item.calendar_day_id, item.exercise_id, item.match_kind): item
        for item in ordered
    }
    placements = [
        CarryOverPlacement(
            calendar_day_id=by_key[key].calendar_day_id,
            target_date=by_key[key].target_date,
            exercise_id=by_key[key].exercise_id,
            target_exercise_name=by_key[key].target_exercise_name,
            target_day_name=by_key[key].target_day_name,
            delta_sets=count,
            match_kind=by_key[key].match_kind,
        )
        for key, count in allocated.items()
    ]
    placements.sort(key=lambda item: (item.target_date, item.calendar_day_id))

    reason = (
        _discard_reason(ordered, session_remaining, mrv_remaining)
        if ordered
        else empty_reason
    )
    return CarryOverDistribution(
        requested_sets=requested_sets,
        placements=placements,
        discarded_reasons=[reason for _ in range(remaining_sets)],
    )


def distribute_carry_over_sources(
    requests: list[CarryOverSourceRequest],
) -> list[SourceCarryOverDistribution]:
    """Coordinate per-source allocation against shared day and muscle budgets."""

    session_remaining: dict[tuple[int, str], int] = {}
    mrv_remaining: dict[str, int] = {}
    for request in requests:
        for candidate in request.candidates:
            day_key = (candidate.calendar_day_id, candidate.muscle)
            session_capacity = max(0, int(candidate.session_cap_remaining))
            session_remaining[day_key] = min(
                session_capacity,
                session_remaining.get(day_key, session_capacity),
            )
            mrv_capacity = max(0, int(candidate.muscle_mrv_remaining))
            mrv_remaining[candidate.muscle] = min(
                mrv_capacity,
                mrv_remaining.get(candidate.muscle, mrv_capacity),
            )

    results: list[SourceCarryOverDistribution] = []
    for request in requests:
        adjusted = [
            CarryOverCandidate(
                **{
                    **candidate.__dict__,
                    "session_cap_remaining": session_remaining.get(
                        (candidate.calendar_day_id, candidate.muscle), 0
                    ),
                    "muscle_mrv_remaining": mrv_remaining.get(
                        candidate.muscle, 0
                    ),
                }
            )
            for candidate in request.candidates
        ]
        distribution = distribute_carry_over(
            request.requested_sets,
            adjusted,
            empty_reason=request.empty_reason,
        )
        for placement in distribution.placements:
            day_key = (placement.calendar_day_id, request.muscle)
            session_remaining[day_key] = max(
                0, session_remaining.get(day_key, 0) - placement.delta_sets
            )
        mrv_remaining[request.muscle] = max(
            0,
            mrv_remaining.get(request.muscle, 0) - distribution.placed_sets,
        )
        results.append(
            SourceCarryOverDistribution(
                source_exercise_id=request.source_exercise_id,
                source_exercise_name=request.source_exercise_name,
                exercise_id=request.exercise_id,
                muscle=request.muscle,
                distribution=distribution,
            )
        )
    return results


_LOCATION_EQUIPMENT: dict[str, set[str] | None] = {
    "gym": None,
    "free_weights": {
        "barbell",
        "dumbbell",
        "kettlebell",
        "bench",
        "pullup_bar",
        "dip_bars",
        "bodyweight",
    },
    "home": {"dumbbell", "kettlebell", "band", "bodyweight", "pullup_bar"},
}


def _allowed_equipment(settings: dict | None) -> set[str] | None:
    locations = list((settings or {}).get("locations") or ["gym"])
    if "gym" in locations:
        return None
    allowed: set[str] = set()
    for location in locations:
        equipment = _LOCATION_EQUIPMENT.get(str(location))
        if equipment is None:
            return None
        allowed.update(equipment)
    return allowed or None


def _equipment_is_available(exercise, allowed: set[str] | None) -> bool:
    if allowed is None:
        return True
    required = set(normalize_equipment_list(exercise.equipment_needed or []))
    return required.issubset(allowed)


async def _current_readiness_verdict(
    db: AsyncSession, app_user_id: int, now: datetime
):
    from api.services.models import UserObservation

    recent_since = now - timedelta(hours=readiness_params.CHECKIN_REASK_HOURS)
    pain_since = now - timedelta(days=readiness_params.PAIN_ACTIVE_DAYS)
    rows = (
        await db.execute(
            select(UserObservation)
            .where(
                UserObservation.app_user_id == app_user_id,
                UserObservation.observed_at >= pain_since,
            )
            .order_by(UserObservation.observed_at.asc(), UserObservation.id.asc())
        )
    ).scalars().all()

    latest: dict[tuple[str, str | None], UserObservation] = {}
    for row in rows:
        if (
            row.kind != readiness_params.KIND_PAIN
            and row.observed_at < recent_since
        ):
            continue
        latest[(row.kind, row.subject)] = row

    sleep = stress = None
    soreness: dict[str, int] = {}
    pain: dict[str, int] = {}
    observed_at = None
    for (kind, subject), row in latest.items():
        observed_at = max(observed_at, row.observed_at) if observed_at else row.observed_at
        if kind == readiness_params.KIND_SLEEP:
            sleep = int(row.value)
        elif kind == readiness_params.KIND_STRESS:
            stress = int(row.value)
        elif kind == readiness_params.KIND_SORENESS and subject:
            soreness[subject] = int(row.value)
        elif kind == readiness_params.KIND_PAIN and subject:
            pain[subject] = int(row.value)
    return build_verdict(
        CheckinSignals(
            sleep=sleep,
            stress=stress,
            soreness=soreness,
            pain=pain,
            observed_at=observed_at,
        )
    )


def _microcycle_end(source_day, days, block) -> date:
    next_start = next(
        (
            day.target_date
            for day in days
            if day.target_date > source_day.target_date
            and day.microcycle_day_number == 1
        ),
        None,
    )
    if next_start is not None:
        return next_start - timedelta(days=1)
    day_number = max(1, int(source_day.microcycle_day_number or 1))
    calculated = source_day.target_date + timedelta(
        days=max(0, int(block.microcycle_length) - day_number)
    )
    return min(calculated, block.planned_end_date)


def _microcycle_start(source_day, days, block) -> date:
    starts = [
        day.target_date
        for day in days
        if day.target_date <= source_day.target_date
        and day.microcycle_day_number == 1
    ]
    if starts:
        return max(starts)
    day_number = max(1, int(source_day.microcycle_day_number or 1))
    calculated = source_day.target_date - timedelta(days=day_number - 1)
    return max(calculated, block.start_date)


def _phase_is_deload(day, block) -> bool:
    if str(day.meso_tag or "").lower() == "deload":
        return True
    phase_number = day.mesocycle_phase_number
    for phase in block.phases or []:
        if phase.get("phase_number") == phase_number:
            return str(phase.get("effort_tier") or "").lower() == "deload"
    return False


def _target_day_name(day_name: str | None, index: int) -> str:
    """Keep the scheduled day label chosen when the preview is built."""

    explicit_name = str(day_name or "").strip()
    return explicit_name or f"Тренировка {index + 1}"


async def preview_carry_over(
    db: AsyncSession,
    user_id: int,
    session_id: int,
    unfinished_sets: Mapping[int, int],
    now: datetime,
) -> CarryOverPreview:
    """Load safe candidates and distribute unfinished sets inside one microcycle."""

    from api.services.models import (
        AppUserProfile,
        Exercise,
        TrainingBlock,
        UserCalendarDay,
        WorkoutPlan,
        WorkoutPlanExercise,
        WorkoutSession,
        WorkoutSessionExercise,
        WorkoutSessionSet,
    )

    source_session = (
        await db.execute(
            select(WorkoutSession).where(
                WorkoutSession.id == session_id,
                WorkoutSession.app_user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if source_session is None:
        raise LookupError("workout session not found")
    if source_session.status != "finished":
        raise ValueError("carry-over requires a finished workout")
    if source_session.calendar_day_id is None or source_session.training_block_id is None:
        raise ValueError("carry-over requires a scheduled workout in a training block")

    source_day = await db.get(UserCalendarDay, source_session.calendar_day_id)
    block = await db.get(TrainingBlock, source_session.training_block_id)
    if (
        source_day is None
        or block is None
        or source_day.app_user_id != user_id
        or block.app_user_id != user_id
    ):
        raise LookupError("source calendar context not found")

    block_days = (
        await db.execute(
            select(UserCalendarDay)
            .where(
                UserCalendarDay.app_user_id == user_id,
                UserCalendarDay.block_id == block.id,
            )
            .order_by(UserCalendarDay.target_date, UserCalendarDay.id)
        )
    ).scalars().all()
    start = _microcycle_start(source_day, block_days, block)
    end = _microcycle_end(source_day, block_days, block)
    cycle_days = [
        day for day in block_days if start <= day.target_date <= end
    ]
    future_days = [
        day
        for day in cycle_days
        if day.target_date > max(source_day.target_date, now.date())
        and day.status == "planned"
        and not day.is_rest_day
        and not day.is_blackout
        and day.actual_workout_session_id is None
        and day.plan_id is not None
    ]

    if future_days:
        active_day_ids = set(
            (
                await db.execute(
                    select(WorkoutSession.calendar_day_id).where(
                        WorkoutSession.app_user_id == user_id,
                        WorkoutSession.calendar_day_id.in_([day.id for day in future_days]),
                        WorkoutSession.status.in_(["active", "paused"]),
                    )
                )
            ).scalars().all()
        )
        future_days = [day for day in future_days if day.id not in active_day_ids]

    source_exercises = (
        await db.execute(
            select(WorkoutSessionExercise, Exercise)
            .join(Exercise, Exercise.id == WorkoutSessionExercise.exercise_id)
            .where(
                WorkoutSessionExercise.workout_session_id == session_id,
                WorkoutSessionExercise.id.in_(list(unfinished_sets)),
            )
            .order_by(WorkoutSessionExercise.order_index, WorkoutSessionExercise.id)
        )
    ).all()
    if len(source_exercises) != len(unfinished_sets):
        raise ValueError("unfinished sets contain an exercise outside the source session")

    plan_ids = {day.plan_id for day in cycle_days if day.plan_id is not None}
    plan_rows = (
        await db.execute(
            select(WorkoutPlanExercise, Exercise, WorkoutPlan)
            .join(Exercise, Exercise.id == WorkoutPlanExercise.exercise_id)
            .join(WorkoutPlan, WorkoutPlan.id == WorkoutPlanExercise.plan_id)
            .where(WorkoutPlanExercise.plan_id.in_(plan_ids))
            .order_by(WorkoutPlanExercise.order_index, WorkoutPlanExercise.id)
        )
    ).all() if plan_ids else []
    rows_by_plan: dict[int, list[tuple]] = {}
    for plan_exercise, exercise, plan in plan_rows:
        rows_by_plan.setdefault(plan.id, []).append((plan_exercise, exercise, plan))

    actual_rows = (
        await db.execute(
            select(
                WorkoutSession.calendar_day_id,
                Exercise,
                func.count(WorkoutSessionSet.id),
            )
            .join(
                WorkoutSessionExercise,
                WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
            )
            .join(Exercise, Exercise.id == WorkoutSessionExercise.exercise_id)
            .join(
                WorkoutSessionSet,
                WorkoutSessionSet.workout_session_exercise_id
                == WorkoutSessionExercise.id,
            )
            .where(
                WorkoutSession.app_user_id == user_id,
                WorkoutSession.calendar_day_id.in_([day.id for day in cycle_days]),
                WorkoutSession.status == "finished",
                WorkoutSessionSet.is_completed.is_(True),
                WorkoutSessionSet.set_type == "normal",
            )
            .group_by(WorkoutSession.calendar_day_id, Exercise.id)
        )
    ).all() if cycle_days else []
    actual_by_day_muscle: dict[tuple[int, str], int] = {}
    for calendar_day_id, actual_exercise, completed_sets in actual_rows:
        muscle = to_system_key(actual_exercise.main_muscle_group)
        if not muscle:
            continue
        actual_by_day_muscle[(calendar_day_id, muscle)] = (
            actual_by_day_muscle.get((calendar_day_id, muscle), 0)
            + int(completed_sets)
        )
    finished_day_ids = set(
        (
            await db.execute(
                select(WorkoutSession.calendar_day_id).where(
                    WorkoutSession.app_user_id == user_id,
                    WorkoutSession.calendar_day_id.in_([day.id for day in cycle_days]),
                    WorkoutSession.status == "finished",
                )
            )
        ).scalars().all()
    ) if cycle_days else set()

    planned_by_day_muscle: dict[tuple[int, str], int] = {}
    planned_by_muscle: dict[str, int] = {}
    for day in cycle_days:
        if day.id in finished_day_ids:
            for (calendar_day_id, muscle), sets in actual_by_day_muscle.items():
                if calendar_day_id == day.id:
                    planned_by_day_muscle[(day.id, muscle)] = sets
                    planned_by_muscle[muscle] = (
                        planned_by_muscle.get(muscle, 0) + sets
                    )
            continue
        day_plan_rows = rows_by_plan.get(day.plan_id, [])
        effective_sets = _effective_plan_sets(
            [
                (plan_exercise.exercise_id, int(plan_exercise.target_sets or 0))
                for plan_exercise, _, _ in day_plan_rows
            ],
            day.volume_adjustments,
        )
        for (plan_exercise, exercise, _), sets in zip(
            day_plan_rows, effective_sets, strict=True
        ):
            muscle = to_system_key(exercise.main_muscle_group)
            if not muscle:
                continue
            planned_by_day_muscle[(day.id, muscle)] = (
                planned_by_day_muscle.get((day.id, muscle), 0) + sets
            )
            planned_by_muscle[muscle] = planned_by_muscle.get(muscle, 0) + sets

    profile = (
        await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == user_id)
        )
    ).scalar_one_or_none()
    level = str(getattr(profile, "experience_level", None) or "beginner").lower()
    if level not in SESSION_MAX:
        level = "beginner"
    allowed_equipment = _allowed_equipment(getattr(profile, "settings", None))
    readiness_verdict = await _current_readiness_verdict(db, user_id, now)

    source_requests: list[CarryOverSourceRequest] = []
    for source_exercise, exercise in source_exercises:
        requested = max(0, int(unfinished_sets[source_exercise.id]))
        muscle = to_system_key(exercise.main_muscle_group) or ""
        landmarks = landmarks_for(muscle, level)
        scaled = (
            scale_landmarks(landmarks, block.microcycle_length / 7.0)
            if landmarks is not None
            else None
        )
        mrv_remaining = max(
            0,
            int(scaled.mrv_direct if scaled is not None else 0)
            - planned_by_muscle.get(muscle, 0),
        )

        candidates: list[CarryOverCandidate] = []
        for day in future_days:
            rows = rows_by_plan.get(day.plan_id, [])
            exact = [row for row in rows if row[0].exercise_id == exercise.id]
            compatible = [
                row
                for row in rows
                if row[0].exercise_id != exercise.id
                and to_system_key(row[1].main_muscle_group) == muscle
            ]
            matches = exact or compatible
            if not matches:
                continue
            target_plan_exercise, target_exercise, _ = matches[0]
            target_readiness = level_for_exercise(
                readiness_verdict, build_exercise_target(target_exercise)
            )
            candidates.append(
                CarryOverCandidate(
                    calendar_day_id=day.id,
                    target_date=day.target_date,
                    exercise_id=target_plan_exercise.exercise_id,
                    target_exercise_name=target_exercise.name,
                    target_day_name=_target_day_name(
                        day.day_tag,
                        max(0, int(day.microcycle_day_number or 1) - 1),
                    ),
                    muscle=muscle,
                    match_kind="exact" if exact else "compatible",
                    session_cap_remaining=max(
                        0,
                        SESSION_MAX[level]
                        - planned_by_day_muscle.get((day.id, muscle), 0),
                    ),
                    muscle_mrv_remaining=mrv_remaining,
                    is_deload=_phase_is_deload(day, block),
                    readiness_level=(
                        "ok"
                        if target_readiness.source == readiness_params.SOURCE_PAIN
                        else target_readiness.level
                    ),
                    has_pain=(target_readiness.source == readiness_params.SOURCE_PAIN),
                    equipment_available=_equipment_is_available(
                        target_exercise, allowed_equipment
                    ),
                )
            )

        source_requests.append(
            CarryOverSourceRequest(
                source_exercise_id=source_exercise.id,
                source_exercise_name=exercise.name,
                exercise_id=exercise.id,
                muscle=muscle,
                requested_sets=requested,
                candidates=candidates,
                empty_reason=(
                    "no_compatible_day" if future_days else "microcycle_end"
                ),
            )
        )

    return CarryOverPreview(
        source_session_id=source_session.id,
        microcycle_end=end,
        items=distribute_carry_over_sources(source_requests),
    )


class CarryOverWorkflowError(Exception):
    pass


class CarryOverSourceNotFound(CarryOverWorkflowError):
    pass


class InvalidUnfinishedSets(CarryOverWorkflowError):
    pass


class CarryOverIdempotencyConflict(CarryOverWorkflowError):
    pass


class CarryOverDecisionConflict(CarryOverWorkflowError):
    pass


class CarryOverRetryableConflict(CarryOverWorkflowError):
    pass


class StaleCarryOverPreview(CarryOverWorkflowError):
    def __init__(self, preview: CarryOverResponse):
        self.preview = preview


def _placement_payload(
    distribution: CarryOverDistribution,
    snapshots: list[dict] | None = None,
) -> list[dict]:
    snapshot_by_target = {
        (
            int(snapshot["calendar_day_id"]),
            int(snapshot["exercise_id"]),
            snapshot["match_kind"],
        ): snapshot
        for snapshot in snapshots or []
    }
    payload: list[dict] = []
    for item in distribution.placements:
        snapshot = snapshot_by_target.get(
            (item.calendar_day_id, item.exercise_id, item.match_kind)
        )
        payload.append(
            {
                "calendar_day_id": item.calendar_day_id,
                "target_date": item.target_date.isoformat(),
                "exercise_id": item.exercise_id,
                "target_exercise_name": (
                    snapshot["target_exercise_name"]
                    if snapshot is not None
                    else item.target_exercise_name
                ),
                "target_day_name": (
                    snapshot["target_day_name"]
                    if snapshot is not None
                    else item.target_day_name
                ),
                "delta_sets": item.delta_sets,
                "match_kind": item.match_kind,
            }
        )
    return payload


def _discarded_payload(distribution: CarryOverDistribution) -> list[dict]:
    counts = Counter(distribution.discarded_reasons)
    return [
        {"sets": counts[reason], "reason": reason}
        for reason in sorted(counts)
    ]


def _item_schema(row) -> CarryOverPreviewItem:
    return CarryOverPreviewItem(
        carry_over_id=row.id,
        source_exercise_id=row.source_exercise_id,
        source_exercise_name=row.source_exercise_name,
        requested_sets=row.requested_sets,
        placed_sets=row.placed_sets,
        discarded_sets=row.discarded_sets,
        placements=row.placements,
        discarded=row.discarded,
    )


def _response_from_rows(rows: list, status: str | None = None) -> CarryOverResponse:
    first = rows[0]
    return CarryOverResponse(
        status=status or first.status,
        source_session_id=first.source_session_id,
        microcycle_end=first.microcycle_end,
        preview_token=first.preview_token,
        items=[_item_schema(row) for row in rows],
    )


async def _validate_unfinished_sets(
    db: AsyncSession,
    user_id: int,
    session_id: int,
    unfinished_sets: Mapping[int, int],
) -> None:
    from api.services.models import (
        WorkoutSession,
        WorkoutSessionExercise,
        WorkoutSessionSet,
    )

    owned = await db.scalar(
        select(WorkoutSession.id).where(
            WorkoutSession.id == session_id,
            WorkoutSession.app_user_id == user_id,
            WorkoutSession.status == "finished",
        )
    )
    if owned is None:
        raise CarryOverSourceNotFound

    rows = (
        await db.execute(
            select(
                WorkoutSessionExercise.id,
                WorkoutSessionExercise.target_sets,
                func.count(WorkoutSessionSet.id),
            )
            .outerjoin(
                WorkoutSessionSet,
                (WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id)
                & WorkoutSessionSet.is_completed.is_(True)
                & (WorkoutSessionSet.set_type == "normal"),
            )
            .where(
                WorkoutSessionExercise.workout_session_id == session_id,
                WorkoutSessionExercise.id.in_(list(unfinished_sets)),
            )
            .group_by(
                WorkoutSessionExercise.id,
                WorkoutSessionExercise.target_sets,
            )
        )
    ).all()
    if len(rows) != len(unfinished_sets):
        raise InvalidUnfinishedSets
    for source_exercise_id, target_sets, completed_sets in rows:
        actual = max(0, int(target_sets or 0) - int(completed_sets or 0))
        if actual <= 0 or actual != int(unfinished_sets[source_exercise_id]):
            raise InvalidUnfinishedSets


async def _preview_state_token(
    db: AsyncSession,
    user_id: int,
    preview: CarryOverPreview,
    now: datetime,
) -> str:
    from api.services.models import (
        AppUserProfile,
        Exercise,
        UserCalendarDay,
        UserObservation,
        TrainingBlock,
        WorkoutPlan,
        WorkoutPlanExercise,
        WorkoutSession,
        WorkoutSessionExercise,
        WorkoutSessionSet,
    )

    source_session = await db.get(WorkoutSession, preview.source_session_id)
    source_day = await db.get(UserCalendarDay, source_session.calendar_day_id)
    block = await db.get(TrainingBlock, source_session.training_block_id)
    block_days = (
        await db.execute(
            select(UserCalendarDay)
            .where(
                UserCalendarDay.app_user_id == user_id,
                UserCalendarDay.block_id == source_day.block_id,
            )
            .order_by(UserCalendarDay.target_date, UserCalendarDay.id)
        )
    ).scalars().all()
    cycle_start = _microcycle_start(source_day, block_days, block)
    calendar_days = (
        await db.execute(
            select(UserCalendarDay)
            .where(
                UserCalendarDay.app_user_id == user_id,
                UserCalendarDay.block_id == source_day.block_id,
                UserCalendarDay.target_date >= cycle_start,
                UserCalendarDay.target_date <= preview.microcycle_end,
            )
            .order_by(UserCalendarDay.target_date, UserCalendarDay.id)
        )
    ).scalars().all()
    plan_ids = sorted({day.plan_id for day in calendar_days if day.plan_id is not None})
    plans = (
        await db.execute(
            select(WorkoutPlan)
            .where(WorkoutPlan.id.in_(plan_ids))
            .order_by(WorkoutPlan.id)
        )
    ).scalars().all() if plan_ids else []
    plan_exercises = (
        await db.execute(
            select(WorkoutPlanExercise)
            .where(WorkoutPlanExercise.plan_id.in_(plan_ids))
            .order_by(
                WorkoutPlanExercise.plan_id,
                WorkoutPlanExercise.order_index,
                WorkoutPlanExercise.id,
            )
        )
    ).scalars().all() if plan_ids else []
    source_exercise_ids = sorted(item.source_exercise_id for item in preview.items)
    source_exercises = (
        await db.execute(
            select(WorkoutSessionExercise)
            .where(WorkoutSessionExercise.id.in_(source_exercise_ids))
            .order_by(WorkoutSessionExercise.id)
        )
    ).scalars().all()
    completed = (
        await db.execute(
            select(
                WorkoutSessionSet.workout_session_exercise_id,
                func.count(WorkoutSessionSet.id),
            )
            .where(
                WorkoutSessionSet.workout_session_exercise_id.in_(source_exercise_ids),
                WorkoutSessionSet.is_completed.is_(True),
                WorkoutSessionSet.set_type == "normal",
            )
            .group_by(WorkoutSessionSet.workout_session_exercise_id)
            .order_by(WorkoutSessionSet.workout_session_exercise_id)
        )
    ).all()
    cycle_completed = (
        await db.execute(
            select(
                WorkoutSession.calendar_day_id,
                WorkoutSessionExercise.exercise_id,
                func.count(WorkoutSessionSet.id),
            )
            .join(
                WorkoutSessionExercise,
                WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
            )
            .join(
                WorkoutSessionSet,
                WorkoutSessionSet.workout_session_exercise_id
                == WorkoutSessionExercise.id,
            )
            .where(
                WorkoutSession.app_user_id == user_id,
                WorkoutSession.calendar_day_id.in_([day.id for day in calendar_days]),
                WorkoutSession.status == "finished",
                WorkoutSessionSet.is_completed.is_(True),
                WorkoutSessionSet.set_type == "normal",
            )
            .group_by(
                WorkoutSession.calendar_day_id,
                WorkoutSessionExercise.exercise_id,
            )
            .order_by(
                WorkoutSession.calendar_day_id,
                WorkoutSessionExercise.exercise_id,
            )
        )
    ).all() if calendar_days else []
    exercise_ids = sorted(
        {item.exercise_id for item in source_exercises}
        | {item.exercise_id for item in plan_exercises}
        | {int(item[1]) for item in cycle_completed}
    )
    exercises = (
        await db.execute(
            select(Exercise).where(Exercise.id.in_(exercise_ids)).order_by(Exercise.id)
        )
    ).scalars().all() if exercise_ids else []
    profile = await db.scalar(
        select(AppUserProfile).where(AppUserProfile.app_user_id == user_id)
    )
    observations = (
        await db.execute(
            select(UserObservation)
            .where(
                UserObservation.app_user_id == user_id,
                UserObservation.observed_at
                >= now - timedelta(days=readiness_params.PAIN_ACTIVE_DAYS),
            )
            .order_by(UserObservation.observed_at, UserObservation.id)
        )
    ).scalars().all()

    payload = {
        "as_of_date": now.date().isoformat(),
        "source_session": {
            "id": source_session.id,
            "status": source_session.status,
            "revision": source_session.revision,
            "updated_at": source_session.updated_at.isoformat(),
            "calendar_day_id": source_session.calendar_day_id,
            "training_block_id": source_session.training_block_id,
        },
        "source_exercises": [
            {
                "id": item.id,
                "exercise_id": item.exercise_id,
                "target_sets": item.target_sets,
                "updated_at": item.updated_at.isoformat(),
            }
            for item in source_exercises
        ],
        "completed_normal_sets": [list(item) for item in completed],
        "cycle_completed_normal_sets": [list(item) for item in cycle_completed],
        "calendar": [
            {
                "id": day.id,
                "target_date": day.target_date.isoformat(),
                "plan_id": day.plan_id,
                "status": day.status,
                "actual_workout_session_id": day.actual_workout_session_id,
                "is_rest_day": day.is_rest_day,
                "is_blackout": day.is_blackout,
                "microcycle_day_number": day.microcycle_day_number,
                "mesocycle_phase_number": day.mesocycle_phase_number,
                "meso_tag": day.meso_tag,
                "volume_adjustments": day.volume_adjustments,
            }
            for day in calendar_days
        ],
        "plans": [
            {
                "id": plan.id,
                "revision": plan.revision,
                "is_archived": plan.is_archived,
            }
            for plan in plans
        ],
        "plan_exercises": [
            {
                "id": item.id,
                "plan_id": item.plan_id,
                "exercise_id": item.exercise_id,
                "order_index": item.order_index,
                "target_sets": item.target_sets,
            }
            for item in plan_exercises
        ],
        "safety": {
            "block": {
                "id": block.id,
                "microcycle_length": block.microcycle_length,
                "planned_end_date": block.planned_end_date.isoformat(),
                "phases": block.phases,
            },
            "experience_level": getattr(profile, "experience_level", None),
            "settings": getattr(profile, "settings", None),
            "exercises": [
                {
                    "id": item.id,
                    "main_muscle_group": item.main_muscle_group,
                    "equipment_needed": item.equipment_needed,
                }
                for item in exercises
            ],
            "observations": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "value": item.value,
                    "subject": item.subject,
                    "observed_at": item.observed_at.isoformat(),
                }
                for item in observations
            ],
        },
        "microcycle_end": preview.microcycle_end.isoformat(),
        "distribution": [
            {
                "source_exercise_id": item.source_exercise_id,
                "exercise_id": item.exercise_id,
                "muscle": item.muscle,
                "requested_sets": item.distribution.requested_sets,
                "placements": _placement_payload(item.distribution),
                "discarded_reasons": item.distribution.discarded_reasons,
            }
            for item in preview.items
        ],
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _derived_idempotency_key(request_key: str, source_exercise_id: int) -> str:
    return hashlib.sha256(
        f"{request_key}:{source_exercise_id}".encode("utf-8")
    ).hexdigest()


async def _lock_owned_source(
    db: AsyncSession, user_id: int, session_id: int
) -> None:
    from api.services.models import WorkoutSession

    owned = await db.scalar(
        select(WorkoutSession.id)
        .where(
            WorkoutSession.id == session_id,
            WorkoutSession.app_user_id == user_id,
        )
        .with_for_update()
    )
    if owned is None:
        raise CarryOverSourceNotFound


async def create_carry_over_preview(
    db: AsyncSession,
    user_id: int,
    session_id: int,
    unfinished_sets: Mapping[int, int],
    idempotency_key: str,
    now: datetime,
) -> CarryOverResponse:
    from api.services.models import WorkoutCarryOver

    # Common per-source lock makes concurrent retries observe the first
    # request's audit rows instead of racing into the uniqueness constraint.
    await _lock_owned_source(db, user_id, session_id)
    existing_rows = (
        await db.execute(
            select(WorkoutCarryOver)
            .where(
                WorkoutCarryOver.app_user_id == user_id,
                WorkoutCarryOver.source_session_id == session_id,
            )
            .order_by(WorkoutCarryOver.source_exercise_id)
            .with_for_update()
        )
    ).scalars().all()
    await _validate_unfinished_sets(db, user_id, session_id, unfinished_sets)
    if existing_rows:
        if {
            row.source_exercise_id for row in existing_rows
        } != set(unfinished_sets):
            raise InvalidUnfinishedSets
        for row in existing_rows:
            if row.status != "draft":
                raise CarryOverDecisionConflict
            if row.idempotency_key != _derived_idempotency_key(
                idempotency_key, row.source_exercise_id
            ):
                raise CarryOverIdempotencyConflict
            if row.requested_sets != int(unfinished_sets[row.source_exercise_id]):
                raise InvalidUnfinishedSets
        return _response_from_rows(existing_rows, status="draft")
    try:
        preview = await preview_carry_over(
            db, user_id, session_id, unfinished_sets, now
        )
    except LookupError as exc:
        raise CarryOverSourceNotFound from exc
    token = await _preview_state_token(db, user_id, preview, now)
    existing_by_source = {row.source_exercise_id: row for row in existing_rows}
    rows: list[WorkoutCarryOver] = []
    for item in preview.items:
        derived_key = _derived_idempotency_key(
            idempotency_key, item.source_exercise_id
        )
        row = existing_by_source.get(item.source_exercise_id)
        if row is not None:
            if row.status != "draft":
                raise CarryOverDecisionConflict
            if row.idempotency_key != derived_key:
                raise CarryOverIdempotencyConflict
            if row.requested_sets != item.distribution.requested_sets:
                raise InvalidUnfinishedSets
        else:
            row = WorkoutCarryOver(
                app_user_id=user_id,
                source_session_id=session_id,
                source_exercise_id=item.source_exercise_id,
                source_exercise_name=item.source_exercise_name,
                requested_sets=item.distribution.requested_sets,
                placed_sets=0,
                discarded_sets=item.distribution.requested_sets,
                placements=[],
                discarded=[
                    {
                        "sets": item.distribution.requested_sets,
                        "reason": "microcycle_end",
                    }
                ],
                microcycle_end=preview.microcycle_end,
                preview_token=token,
                idempotency_key=derived_key,
                status="draft",
            )
            db.add(row)
        row.placed_sets = item.distribution.placed_sets
        row.discarded_sets = item.distribution.discarded_sets
        row.placements = _placement_payload(item.distribution)
        row.discarded = _discarded_payload(item.distribution)
        row.microcycle_end = preview.microcycle_end
        row.preview_token = token
        rows.append(row)
    await db.flush()
    await db.commit()
    return _response_from_rows(rows, status="draft")


async def _carry_over_rows_for_update(
    db: AsyncSession, user_id: int, session_id: int
) -> list:
    from api.services.models import WorkoutCarryOver

    rows = (
        await db.execute(
            select(WorkoutCarryOver)
            .where(
                WorkoutCarryOver.app_user_id == user_id,
                WorkoutCarryOver.source_session_id == session_id,
            )
            .order_by(WorkoutCarryOver.source_exercise_id)
            .with_for_update()
        )
    ).scalars().all()
    if not rows:
        raise CarryOverSourceNotFound
    return rows


async def _lock_carry_over_context(
    db: AsyncSession, user_id: int, session_id: int
) -> None:
    from api.services.models import (
        AppUserProfile,
        Exercise,
        TrainingBlock,
        UserCalendarDay,
        UserObservation,
        WorkoutPlan,
        WorkoutPlanExercise,
        WorkoutSession,
        WorkoutSessionExercise,
    )

    source_session = (
        await db.execute(
            select(WorkoutSession)
            .where(
                WorkoutSession.id == session_id,
                WorkoutSession.app_user_id == user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if source_session is None:
        raise CarryOverSourceNotFound
    await db.execute(
        select(TrainingBlock.id)
        .where(TrainingBlock.id == source_session.training_block_id)
        .with_for_update()
    )
    await db.execute(
        select(AppUserProfile.id)
        .where(AppUserProfile.app_user_id == user_id)
        .with_for_update()
    )
    await db.execute(
        select(UserObservation.id)
        .where(UserObservation.app_user_id == user_id)
        .with_for_update()
    )
    await db.execute(
        select(WorkoutSessionExercise.id)
        .where(WorkoutSessionExercise.workout_session_id == session_id)
        .with_for_update()
    )
    days = (
        await db.execute(
            select(UserCalendarDay)
            .where(
                UserCalendarDay.app_user_id == user_id,
                UserCalendarDay.block_id == source_session.training_block_id,
            )
            .with_for_update()
        )
    ).scalars().all()
    plan_ids = {day.plan_id for day in days if day.plan_id is not None}
    if plan_ids:
        await db.execute(
            select(WorkoutPlan.id)
            .where(WorkoutPlan.id.in_(plan_ids))
            .with_for_update()
        )
        await db.execute(
            select(WorkoutPlanExercise.id)
            .where(WorkoutPlanExercise.plan_id.in_(plan_ids))
            .with_for_update()
        )
    exercise_ids = set(
        (
            await db.execute(
                select(WorkoutSessionExercise.exercise_id).where(
                    WorkoutSessionExercise.workout_session_id == session_id
                )
            )
        ).scalars().all()
    )
    if plan_ids:
        exercise_ids.update(
            (
                await db.execute(
                    select(WorkoutPlanExercise.exercise_id).where(
                        WorkoutPlanExercise.plan_id.in_(plan_ids)
                    )
                )
            ).scalars().all()
        )
    if exercise_ids:
        await db.execute(
            select(Exercise.id)
            .where(Exercise.id.in_(exercise_ids))
            .with_for_update()
        )


async def _apply_carry_over_once(
    db: AsyncSession,
    user_id: int,
    session_id: int,
    preview_token: str,
    now: datetime,
) -> CarryOverResponse:
    from api.services.models import UserCalendarDay

    # Authentication has already performed a read on this request session,
    # which starts a default transaction. It made no writes, so close it
    # before opening the serializable decision transaction.
    await db.rollback()
    await db.connection(execution_options={"isolation_level": "SERIALIZABLE"})
    # Every decision operation follows the same order: source, draft rows,
    # safety/context.  Observation writers share the advisory safety lock.
    await _lock_owned_source(db, user_id, session_id)
    rows = await _carry_over_rows_for_update(db, user_id, session_id)
    if all(row.status == "applied" for row in rows):
        if rows[0].preview_token != preview_token:
            raise CarryOverDecisionConflict
        response = _response_from_rows(rows, status="applied")
        await db.commit()
        return response
    if any(row.status != "draft" for row in rows):
        raise CarryOverDecisionConflict
    await lock_user_safety_context(db, user_id)
    await _lock_carry_over_context(db, user_id, session_id)

    requested = {row.source_exercise_id: row.requested_sets for row in rows}
    await _validate_unfinished_sets(db, user_id, session_id, requested)
    preview = await preview_carry_over(db, user_id, session_id, requested, now)
    fresh_token = await _preview_state_token(db, user_id, preview, now)
    if preview_token != rows[0].preview_token or fresh_token != rows[0].preview_token:
        by_source = {item.source_exercise_id: item for item in preview.items}
        for row in rows:
            item = by_source[row.source_exercise_id]
            row.placed_sets = item.distribution.placed_sets
            row.discarded_sets = item.distribution.discarded_sets
            row.placements = _placement_payload(item.distribution, row.placements)
            row.discarded = _discarded_payload(item.distribution)
            row.microcycle_end = preview.microcycle_end
            row.preview_token = fresh_token
        await db.commit()
        raise StaleCarryOverPreview(_response_from_rows(rows, status="draft"))

    applied_at = now.astimezone(timezone.utc)
    for row in rows:
        for placement in row.placements:
            day = await db.get(
                UserCalendarDay,
                int(placement["calendar_day_id"]),
                populate_existing=True,
            )
            adjustments = list(day.volume_adjustments or [])
            if not any(item.get("carry_over_id") == row.id for item in adjustments):
                adjustments.append(
                    {
                        "exercise_id": int(placement["exercise_id"]),
                        "delta_sets": int(placement["delta_sets"]),
                        "carry_over_id": row.id,
                        "provenance": {
                            "kind": "carry_over",
                            "source_session_id": session_id,
                            "source_exercise_id": row.source_exercise_id,
                            "preview_token": preview_token,
                        },
                    }
                )
                day.volume_adjustments = adjustments
        row.status = "applied"
        row.applied_at = applied_at
    await db.commit()
    return _response_from_rows(rows, status="applied")


async def apply_carry_over_preview(
    db: AsyncSession,
    user_id: int,
    session_id: int,
    preview_token: str,
    now: datetime,
) -> CarryOverResponse:
    for attempt in range(3):
        try:
            return await _apply_carry_over_once(
                db, user_id, session_id, preview_token, now
            )
        except Exception as exc:
            if not _is_serialization_failure(exc):
                raise
            await db.rollback()
            if attempt == 2:
                raise CarryOverRetryableConflict from exc
    raise AssertionError("unreachable")


async def dismiss_carry_over_preview(
    db: AsyncSession,
    user_id: int,
    session_id: int,
    preview_token: str,
) -> CarryOverResponse:
    await _lock_owned_source(db, user_id, session_id)
    rows = await _carry_over_rows_for_update(db, user_id, session_id)
    if all(row.status == "dismissed" for row in rows):
        if rows[0].preview_token != preview_token:
            raise CarryOverDecisionConflict
        return _response_from_rows(rows, status="dismissed")
    if any(row.status != "draft" for row in rows):
        raise CarryOverDecisionConflict
    if any(row.preview_token != preview_token for row in rows):
        raise CarryOverDecisionConflict
    for row in rows:
        row.status = "dismissed"
    await db.commit()
    return _response_from_rows(rows, status="dismissed")
