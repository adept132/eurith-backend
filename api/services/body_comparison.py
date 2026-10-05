"""Read-only context for two private progress photos."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.body_service import BODY_METRIC_KEYS
from api.services.models import (
    AppUserProfile, BodyMeasurement, Exercise, TrainingBlock, UserAnthropometry,
    WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet,
)
from api.services.progression.records import brzycki_e1rm
from api.services.volume.repository import adherence_for_range


def _row_day(row) -> tuple[date | None, str]:
    if row.measured_on:
        return row.measured_on, "user"
    if row.recorded_at:
        return row.recorded_at.date(), "legacy_utc"
    return None, "unknown"


def nearest_measurement(rows, metric: str, target: date) -> dict | None:
    """Find an explicit measurement within seven days; earlier wins a tie."""
    candidates = []
    for row in rows:
        if isinstance(row, UserAnthropometry) or hasattr(row, "submitted_fields"):
            if row.submitted_fields is None or metric not in row.submitted_fields:
                continue
            value = getattr(row, metric, None)
        else:
            if row.metric_key != metric:
                continue
            value = row.value
        day, date_source = _row_day(row)
        if value is None or day is None or abs((day - target).days) > 7:
            continue
        recorded_at = getattr(row, "recorded_at", None)
        recorded_rank = -(recorded_at.timestamp() if recorded_at else 0)
        candidates.append((abs((day - target).days), day, recorded_rank, float(value), date_source))
    if not candidates:
        return None
    _, day, _, value, source = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    return {"date": day, "value": round(value, 1), "date_source": source}


def intersect_phases(blocks, start: date, end: date) -> list[dict]:
    phases = []
    for block in blocks:
        cursor = block.start_date
        block_end = min(block.planned_end_date, block.actual_end_date or block.planned_end_date)
        for phase in block.phases or []:
            length = int(phase.get("length_days", 0))
            if length <= 0:
                continue
            phase_end = min(cursor + timedelta(days=length - 1), block_end)
            overlap_start, overlap_end = max(cursor, start), min(phase_end, end)
            if overlap_start <= overlap_end:
                phases.append({
                    "block_id": block.id,
                    "phase_number": int(phase["phase_number"]),
                    "name": str(phase.get("name") or "Фаза"),
                    "effort_tier": str(phase.get("effort_tier") or ""),
                    "start_date": overlap_start,
                    "end_date": overlap_end,
                })
            cursor += timedelta(days=length)
            if cursor > block_end:
                break
    return sorted(phases, key=lambda item: (item["start_date"], item["block_id"]))


async def _strength(db: AsyncSession, user_id: int, start: date, end: date) -> list[dict]:
    # Match the inclusion rules used by /progress/achievements.
    zone_name = (await db.execute(select(AppUserProfile.timezone).where(
        AppUserProfile.app_user_id == user_id,
    ))).scalar_one_or_none() or "UTC"
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    start_at = datetime.combine(start, datetime.min.time(), zone).astimezone(timezone.utc)
    end_at = datetime.combine(end + timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc)
    rows = (await db.execute(
        select(WorkoutSession.id, WorkoutSession.finished_at,
               WorkoutSessionExercise.exercise_id, Exercise.name,
               WorkoutSessionSet.weight, WorkoutSessionSet.reps)
        .select_from(WorkoutSessionSet)
        .join(WorkoutSessionExercise)
        .join(WorkoutSession)
        .join(Exercise)
        .where(
            WorkoutSession.app_user_id == user_id,
            WorkoutSession.status == "finished",
            WorkoutSession.finished_at >= start_at,
            WorkoutSession.finished_at < end_at,
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.is_anomalous.is_(False),
            WorkoutSessionSet.set_type == "normal",
            WorkoutSessionSet.weight > 0,
            WorkoutSessionSet.reps > 0,
            WorkoutSessionSet.reps < 37,
        )
        .order_by(WorkoutSession.finished_at, WorkoutSession.id)
    )).all()
    workouts = {}
    for workout_id, finished_at, exercise_id, name, weight, reps in rows:
        estimate = brzycki_e1rm(float(weight), int(reps))
        if estimate is None:
            continue
        key = (exercise_id, workout_id)
        prior = workouts.get(key)
        if prior is None or estimate > prior[2]:
            workouts[key] = (finished_at.astimezone(zone).date(), name, estimate)
    by_exercise = defaultdict(list)
    for (exercise_id, _workout_id), item in workouts.items():
        by_exercise[exercise_id].append(item)
    result = []
    for exercise_id, performances in by_exercise.items():
        performances.sort(key=lambda item: item[0])
        if len({item[0] for item in performances}) < 2:
            continue
        first, last = performances[0], performances[-1]
        result.append({
            "exercise_id": exercise_id, "exercise_name": first[1],
            "first_date": first[0], "last_date": last[0],
            "first_e1rm": round(first[2], 1), "last_e1rm": round(last[2], 1),
            "delta_e1rm": round(last[2] - first[2], 1),
        })
    result.sort(key=lambda item: (-abs(item["delta_e1rm"]), item["exercise_id"]))
    return result[:3]


async def body_comparison(db: AsyncSession, user_id: int, before, after) -> dict:
    start, end = before.taken_on, after.taken_on
    window_start, window_end = start - timedelta(days=7), end + timedelta(days=7)
    utc_start = datetime.combine(window_start, datetime.min.time(), timezone.utc)
    utc_end = datetime.combine(window_end + timedelta(days=1), datetime.min.time(), timezone.utc)
    anthro = (await db.execute(select(UserAnthropometry).where(
        UserAnthropometry.app_user_id == user_id,
        or_(UserAnthropometry.measured_on.between(window_start, window_end),
            (UserAnthropometry.measured_on.is_(None) & UserAnthropometry.recorded_at.between(utc_start, utc_end))),
    ))).scalars().all()
    measurements = (await db.execute(select(BodyMeasurement).where(
        BodyMeasurement.app_user_id == user_id,
        or_(BodyMeasurement.measured_on.between(window_start, window_end),
            (BodyMeasurement.measured_on.is_(None) & BodyMeasurement.recorded_at.between(utc_start, utc_end))),
    ))).scalars().all()
    metrics = {}
    for key in ("weight", "body_fat", *sorted(BODY_METRIC_KEYS)):
        source = anthro if key in ("weight", "body_fat") else measurements
        first = nearest_measurement(source, key, start)
        last = nearest_measurement(source, key, end)
        if first is None and last is None:
            continue
        metrics[key] = {"before": first, "after": last,
                        "delta": round(last["value"] - first["value"], 1) if first and last else None}
    blocks = (await db.execute(select(TrainingBlock).where(
        TrainingBlock.app_user_id == user_id,
        TrainingBlock.start_date <= end,
        TrainingBlock.planned_end_date >= start,
    ))).scalars().all()
    adherence = await adherence_for_range(db, user_id, start, end)
    return {
        "before": {"id": before.id, "taken_on": start, "angle": before.angle},
        "after": {"id": after.id, "taken_on": end, "angle": after.angle},
        "interval": {"start_date": start, "end_date": end},
        "body_metrics": metrics,
        "phases": intersect_phases(blocks, start, end),
        "adherence": {
            "planned_days": adherence.planned_days,
            "completed_days": adherence.completed_days,
            "missed_days": adherence.missed_days,
            "percent": round(100 * adherence.completed_days / adherence.planned_days)
            if adherence.planned_days else None,
        },
        "strength": await _strength(db, user_id, start, end),
    }
