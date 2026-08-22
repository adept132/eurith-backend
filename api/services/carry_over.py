from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.equipment import normalize_equipment_list
from api.services.muscle_keys import to_system_key
from api.services.readiness import params as readiness_params
from api.services.readiness.repository import build_exercise_target
from api.services.readiness.types import CheckinSignals
from api.services.readiness.verdict import build_verdict, level_for_exercise
from api.services.volume.landmarks import SESSION_MAX, landmarks_for, scale_landmarks


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


@dataclass(frozen=True)
class CarryOverCandidate:
    calendar_day_id: int
    target_date: date
    exercise_id: int
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
    exercise_id: int
    muscle: str
    distribution: CarryOverDistribution


@dataclass(frozen=True)
class CarryOverSourceRequest:
    source_exercise_id: int
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


def _phase_is_deload(day, block) -> bool:
    if str(day.meso_tag or "").lower() == "deload":
        return True
    phase_number = day.mesocycle_phase_number
    for phase in block.phases or []:
        if phase.get("phase_number") == phase_number:
            return str(phase.get("effort_tier") or "").lower() == "deload"
    return False


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

    days = (
        await db.execute(
            select(UserCalendarDay)
            .where(
                UserCalendarDay.app_user_id == user_id,
                UserCalendarDay.block_id == block.id,
                UserCalendarDay.target_date >= source_day.target_date,
            )
            .order_by(UserCalendarDay.target_date, UserCalendarDay.id)
        )
    ).scalars().all()
    end = _microcycle_end(source_day, days, block)
    cycle_days = [day for day in days if day.target_date <= end]
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
    exercise_by_id: dict[int, object] = {}
    for plan_exercise, exercise, plan in plan_rows:
        rows_by_plan.setdefault(plan.id, []).append((plan_exercise, exercise, plan))
        exercise_by_id[exercise.id] = exercise

    planned_by_day_muscle: dict[tuple[int, str], int] = {}
    planned_by_muscle: dict[str, int] = {}
    for day in cycle_days:
        for plan_exercise, exercise, _ in rows_by_plan.get(day.plan_id, []):
            muscle = to_system_key(exercise.main_muscle_group)
            if not muscle:
                continue
            sets = int(plan_exercise.target_sets or 0)
            planned_by_day_muscle[(day.id, muscle)] = (
                planned_by_day_muscle.get((day.id, muscle), 0) + sets
            )
            planned_by_muscle[muscle] = planned_by_muscle.get(muscle, 0) + sets
        for adjustment in day.volume_adjustments or []:
            try:
                exercise = exercise_by_id[int(adjustment["exercise_id"])]
                delta = int(adjustment["delta_sets"])
            except (KeyError, TypeError, ValueError):
                continue
            muscle = to_system_key(exercise.main_muscle_group)
            if not muscle:
                continue
            planned_by_day_muscle[(day.id, muscle)] = (
                planned_by_day_muscle.get((day.id, muscle), 0) + delta
            )
            planned_by_muscle[muscle] = planned_by_muscle.get(muscle, 0) + delta

    for source_exercise, exercise in source_exercises:
        muscle = to_system_key(exercise.main_muscle_group)
        if muscle:
            planned_by_muscle[muscle] = max(
                0,
                planned_by_muscle.get(muscle, 0)
                - max(0, int(unfinished_sets[source_exercise.id])),
            )

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
