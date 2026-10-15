"""Capture-time body facts. All writes belong to the caller's transaction."""
from datetime import date, datetime, timezone
from decimal import Decimal
from math import isfinite
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import select

from api.schemas.analytics_context import BodyMassSnapshot, WorkoutAnalyticsSnapshot
from api.services.models import (AppUserProfile, BodyMeasurement, UserAnthropometry,
                                 WorkoutSession, TrainingBlock, UserCalendarDay)
from api.services.body_service import append_body_entry, lock_body_intent


def saved_phase_deload(phases: list | None, phase_number: int | None) -> bool | None:
    """Only recognized saved tiers are evidence; absence is not normal training."""
    matches = [p for p in (phases or []) if isinstance(p, dict)
               and phase_number is not None and p.get('phase_number') == phase_number]
    if len(matches) != 1:
        return None
    return _tier_deload(matches[0].get('effort_tier'))


def _tier_deload(tier: str | None) -> bool | None:
    if tier == 'deload':
        return True
    if tier in {'easy', 'medium', 'hard', 'prefailure', 'failure', 'adaptive'}:
        return False
    return None


async def _capture_phase(db, workout) -> tuple[bool | None, str | None]:
    if workout.training_block_id is not None:
        phases = await db.scalar(select(TrainingBlock.phases).where(
            TrainingBlock.id == workout.training_block_id,
            TrainingBlock.app_user_id == workout.app_user_id))
        flag = saved_phase_deload(phases, workout.mesocycle_phase)
        if flag is not None:
            return flag, 'training_block'
    # Fact linkage, never a date guess or a reusable plan/template lookup.
    days = (await db.execute(select(UserCalendarDay).where(
        UserCalendarDay.app_user_id == workout.app_user_id,
        UserCalendarDay.actual_workout_session_id == workout.id,
        UserCalendarDay.is_rest_day.is_(False), UserCalendarDay.is_blackout.is_(False),
    ))).scalars().all()
    if len(days) == 1:
        day = days[0]
        if ((workout.training_block_id is None or day.block_id == workout.training_block_id)
                and (workout.mesocycle_phase is None or day.mesocycle_phase_number == workout.mesocycle_phase)):
            flag = _tier_deload(day.meso_tag)
            if flag is not None:
                return flag, 'calendar'
    return None, None


async def resolve_body_mass(db, user_id: int, workout_day: date) -> BodyMassSnapshot | None:
    row = (await db.execute(select(UserAnthropometry).where(
        UserAnthropometry.app_user_id == user_id,
        UserAnthropometry.measured_on <= workout_day,
        UserAnthropometry.submitted_fields.contains(['weight']),
        UserAnthropometry.weight > 0,
    ).order_by(UserAnthropometry.measured_on.desc(), UserAnthropometry.recorded_at.desc(),
               UserAnthropometry.id.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return None
    return BodyMassSnapshot(weight_kg=row.weight, measured_on=row.measured_on,
                            measurement_id=row.id, measurement_client_uuid=row.client_uuid,
                            source='journal', captured_at=datetime.now(timezone.utc))


async def append_workout_weighing(db, user_id: int, *, client_uuid: str,
                                 weight_kg: Decimal, measured_on: date) -> int:
    if not client_uuid or len(client_uuid) > 64 or not weight_kg.is_finite() or weight_kg <= 0:
        raise HTTPException(422, 'Invalid weighing intent')
    # Journal and wire weights use float; canonicalize once before any write or
    # replay comparison. Exact equality at that representation, never an epsilon.
    stored_weight = float(weight_kg)
    if not isfinite(stored_weight) or stored_weight <= 0:
        raise HTTPException(422, 'Weighing cannot be represented as a positive finite weight')
    await lock_body_intent(db, user_id, client_uuid)
    row = (await db.execute(select(UserAnthropometry).where(
        UserAnthropometry.app_user_id == user_id,
        UserAnthropometry.client_uuid == client_uuid,
    ))).scalar_one_or_none()
    if row is not None:
        if (row.submitted_fields != ['weight'] or row.weight is None
                or row.measured_on != measured_on or row.weight != stored_weight):
            raise HTTPException(409, 'Weighing UUID already has different data')
        return row.id
    collision = await db.scalar(select(BodyMeasurement.id).where(
        BodyMeasurement.app_user_id == user_id, BodyMeasurement.client_uuid == client_uuid).limit(1))
    if collision is not None:
        raise HTTPException(409, 'Weighing UUID already belongs to another body entry')
    await append_body_entry(db, user_id, weight=stored_weight,
                            client_uuid=client_uuid, measured_on=measured_on)
    return (await db.execute(select(UserAnthropometry.id).where(
        UserAnthropometry.app_user_id == user_id,
        UserAnthropometry.client_uuid == client_uuid))).scalar_one()


async def capture_workout_snapshot(db, workout: WorkoutSession, *,
                                   explicit_mass: BodyMassSnapshot | None,
                                   reconstruct: bool = False) -> WorkoutAnalyticsSnapshot:
    """Call only for new capture, explicit correction or opt-in reconstruction.

    Reads and ordinary rebuilds must consume stored JSON without calling here.
    An existing explicit unknown is as durable as a known mass.
    """
    existing = (WorkoutAnalyticsSnapshot.model_validate(workout.analytics_snapshot)
                if workout.analytics_snapshot is not None else None)
    if existing is not None and explicit_mass is None:
        return existing
    zone_name = await db.scalar(select(AppUserProfile.timezone).where(
        AppUserProfile.app_user_id == workout.app_user_id)) or 'UTC'
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    if workout.started_at is None:
        raise HTTPException(422, 'Workout start is required for body mass capture')
    workout_day = workout.started_at.astimezone(zone).date()
    mass = explicit_mass
    if mass is not None:
        if mass.measured_on > workout_day:
            raise HTTPException(422, 'Body mass cannot postdate the local workout start date')
        if mass.measurement_id is not None or mass.measurement_client_uuid is not None:
            predicates = [UserAnthropometry.app_user_id == workout.app_user_id]
            if mass.measurement_id is not None:
                predicates.append(UserAnthropometry.id == mass.measurement_id)
            if mass.measurement_client_uuid is not None:
                predicates.append(UserAnthropometry.client_uuid == mass.measurement_client_uuid)
            row = (await db.execute(select(UserAnthropometry).where(*predicates))).scalar_one_or_none()
            if (row is None or row.measured_on != mass.measured_on or row.weight != mass.weight_kg
                    or not row.submitted_fields or 'weight' not in row.submitted_fields):
                raise HTTPException(409, 'Body mass reference does not match this owner and fact')
        elif mass.source == 'journal':
            raise HTTPException(422, 'Journal mass requires a local measurement reference')
    else:
        mass = await resolve_body_mass(db, workout.app_user_id, workout_day)
        if reconstruct and mass is not None:
            mass = mass.model_copy(update={'source': 'reconstructed'})
    if existing is None:
        flag, phase_source = await _capture_phase(db, workout)
        existing = WorkoutAnalyticsSnapshot(is_deload=flag, phase_source=phase_source)
    result = existing.model_copy(update={'body_mass': mass})
    workout.analytics_snapshot = result.model_dump(mode='json')
    await db.flush()
    return result
