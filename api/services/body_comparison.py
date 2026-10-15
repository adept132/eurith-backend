"""Read-only context for two private progress photos."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.analytics.context import select_boundary_measurement, body_changes
from api.services.analytics.types import ResolvedPeriod
from api.services.models import (
    AppUserProfile, BodyMeasurement, Exercise, TrainingBlock, UserAnthropometry,
    WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet,
)
from api.services.progression.records import brzycki_e1rm
from api.services.volume.repository import adherence_for_range


def _measurement_rows(rows):
    result = []
    for index, row in enumerate(rows):
        anthro = isinstance(row, UserAnthropometry) or hasattr(row, "submitted_fields")
        common = dict(id=getattr(row, "id", index), source="anthropometry" if anthro else "body_measurement",
                      measured_on=row.measured_on, recorded_at=row.recorded_at)
        if anthro:
            for key in row.submitted_fields or []:
                result.append(dict(common, submitted_fields=row.submitted_fields, metric_key=key,
                                   value=getattr(row, key, None)))
        else:
            result.append(dict(common, metric_key=row.metric_key, value=row.value))
    return result


def nearest_measurement(rows, metric: str, target: date, *, timezone: str = "UTC") -> dict | None:
    """Compatibility adapter for photo callers; shared selection stays unrounded."""
    selected = select_boundary_measurement(_measurement_rows(rows), metric, target, timezone=timezone)
    if selected is None:
        return None
    return {"date": selected["date"], "value": round(selected["value"], 1),
            "date_source": "user" if selected["date_source"] == "measured_on" else selected["date_source"]}


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
    zone_name = (await db.execute(select(AppUserProfile.timezone).where(
        AppUserProfile.app_user_id == user_id,
    ))).scalar_one_or_none() or "UTC"
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        zone_name, zone = "UTC", ZoneInfo("UTC")
    utc_start = datetime.combine(window_start, datetime.min.time(), zone)
    utc_end = datetime.combine(window_end + timedelta(days=1), datetime.min.time(), zone)
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
    period = ResolvedPeriod(start, end, zone_name,
        datetime.combine(start, datetime.min.time(), zone),
        datetime.combine(end + timedelta(days=1), datetime.min.time(), zone), None)
    metrics = body_changes(_measurement_rows([*anthro, *measurements]), period)["metrics"]
    for item in metrics.values():
        if item["delta"] is not None:
            item["delta"] = round(item["delta"], 1)
        for edge in ("before", "after"):
            observation = item[edge]
            if observation:
                item[edge] = {"date": observation["date"], "value": round(observation["value"], 1),
                              "date_source": "user" if observation["date_source"] == "measured_on" else observation["date_source"]}
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
