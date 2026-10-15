import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app.database import SessionLocal
from api.services.models import UserAnthropometry, WorkoutSession, AppUserProfile, TrainingBlock, UserCalendarDay
from api.schemas.analytics_context import BodyMassSnapshot
from api.services.analytics.snapshots import resolve_body_mass, capture_workout_snapshot, append_workout_weighing
from api.services.analytics.state import read_state
from api.services.body_service import record_body_entry

D = lambda n: date(2026, 10, n)

def manual(day=15, weight=79, **kwargs):
    return BodyMassSnapshot(weight_kg=weight, measured_on=D(day), source='manual',
                            captured_at=datetime.now(timezone.utc), **kwargs)

async def workout(db, owner):
    row = WorkoutSession(app_user_id=owner, source='free', status='finished',
                         started_at=datetime(2026, 10, 15, 12, tzinfo=timezone.utc),
                         finished_at=datetime(2026, 10, 15, 13, tzinfo=timezone.utc))
    db.add(row)
    await db.flush()
    return row

@pytest.mark.asyncio
async def test_latest_explicit_before_workout(test_user, db):
    await record_body_entry(db, test_user.id, weight=80, measured_on=D(10))
    await record_body_entry(db, test_user.id, height=180, measured_on=D(14))
    await record_body_entry(db, test_user.id, weight=82, measured_on=D(16))
    mass = await resolve_body_mass(db, test_user.id, D(15))
    assert (mass.weight_kg, mass.measured_on, mass.source) == (80, D(10), 'journal')

@pytest.mark.asyncio
async def test_snapshot_survives_new_weighing(test_user, db):
    await append_workout_weighing(db, test_user.id, client_uuid='first', weight_kg=Decimal(80), measured_on=D(10))
    row = await workout(db, test_user.id)
    initial = await capture_workout_snapshot(db, row, explicit_mass=None)
    await record_body_entry(db, test_user.id, weight=83, measured_on=D(12))
    assert (await read_state(db, test_user.id)).source_revision == 1
    assert await capture_workout_snapshot(db, row, explicit_mass=None) == initial
    assert initial.body_mass.weight_kg == 80

@pytest.mark.asyncio
async def test_concurrent_mass_intent_once(test_user):
    key = str(uuid4())
    async def append():
        async with SessionLocal() as db:
            result = await append_workout_weighing(db, test_user.id, client_uuid=key, weight_kg=Decimal(80), measured_on=D(10))
            await db.commit()
            return result
    a, b = await asyncio.gather(append(), append())
    assert a == b
    async with SessionLocal() as db:
        assert (await db.scalar(select(func.count()).select_from(UserAnthropometry).where(UserAnthropometry.app_user_id == test_user.id))) == 1
        with pytest.raises(HTTPException) as exc:
            await append_workout_weighing(db, test_user.id, client_uuid=key, weight_kg=Decimal(81), measured_on=D(10))
        assert exc.value.status_code == 409

@pytest.mark.asyncio
async def test_historical_override_is_local(test_user, db):
    await append_workout_weighing(db, test_user.id, client_uuid='x', weight_kg=Decimal(80), measured_on=D(10))
    row = await workout(db, test_user.id)
    result = await capture_workout_snapshot(db, row, explicit_mass=manual())
    assert result.body_mass.weight_kg == 79
    assert (await resolve_body_mass(db, test_user.id, D(15))).weight_kg == 80

@pytest.mark.asyncio
async def test_explicit_future_weighing_rejected(test_user, db):
    row = await workout(db, test_user.id)
    with pytest.raises(HTTPException):
        await capture_workout_snapshot(db, row, explicit_mass=manual(16))
    assert row.analytics_snapshot is None
    assert await db.scalar(select(func.count()).select_from(UserAnthropometry).where(UserAnthropometry.app_user_id == test_user.id)) == 0

@pytest.mark.asyncio
async def test_local_start_date_and_immutable_profile(test_user, db):
    profile = AppUserProfile(app_user_id=test_user.id, timezone='Europe/Moscow')
    db.add(profile)
    row = await workout(db, test_user.id)
    row.started_at = datetime(2026, 10, 14, 22, tzinfo=timezone.utc)
    result = await capture_workout_snapshot(db, row, explicit_mass=manual(15))
    profile.timezone = 'America/Los_Angeles'
    assert await capture_workout_snapshot(db, row, explicit_mass=None) == result
    other = await workout(db, test_user.id)
    other.started_at = datetime(2026, 10, 15, 1, tzinfo=timezone.utc)
    with pytest.raises(HTTPException):
        await capture_workout_snapshot(db, other, explicit_mass=manual(15))

@pytest.mark.asyncio
async def test_append_and_snapshot_rollback(test_user, db):
    row = await workout(db, test_user.id)
    await db.commit()
    workout_id = row.id
    await append_workout_weighing(db, test_user.id, client_uuid='rolled-back', weight_kg=Decimal(80), measured_on=D(10))
    await capture_workout_snapshot(db, row, explicit_mass=None)
    await db.rollback()
    assert await db.scalar(select(UserAnthropometry.id).where(UserAnthropometry.app_user_id == test_user.id)) is None
    assert (await db.get(WorkoutSession, workout_id)).analytics_snapshot is None

@pytest.mark.asyncio
async def test_future_intent_rolls_back_complete_mutation(test_user, db):
    row = await workout(db, test_user.id)
    await db.commit()
    with pytest.raises(HTTPException):
        async with db.begin_nested():
            mass_id = await append_workout_weighing(db, test_user.id, client_uuid='future',
                                                   weight_kg=Decimal(80), measured_on=D(16))
            await capture_workout_snapshot(db, row, explicit_mass=manual(16, 80, measurement_id=mass_id))
    assert row.analytics_snapshot is None
    assert await db.scalar(select(UserAnthropometry.id).where(UserAnthropometry.app_user_id == test_user.id)) is None

@pytest.mark.asyncio
async def test_standalone_revision_and_noop_replay(test_user, db):
    await record_body_entry(db, test_user.id, weight=80, measured_on=D(10), client_uuid='journal')
    assert (await read_state(db, test_user.id)).source_revision == 1
    await record_body_entry(db, test_user.id, weight=80, measured_on=D(10), client_uuid='journal')
    assert (await read_state(db, test_user.id)).source_revision == 1
    await db.commit()

@pytest.mark.asyncio
async def test_mass_intent_conflicts_with_non_weight_entry(test_user, db):
    await record_body_entry(db, test_user.id, height=180, measured_on=D(10), client_uuid='height-only')
    with pytest.raises(HTTPException) as exc:
        await append_workout_weighing(db, test_user.id, client_uuid='height-only', weight_kg=Decimal(80), measured_on=D(10))
    assert exc.value.status_code == 409

@pytest.mark.asyncio
async def test_unknown_and_opt_in_reconstruction(test_user, db):
    db.add(UserAnthropometry(app_user_id=test_user.id, weight=90, measured_on=D(10), submitted_fields=None))
    unknown = await workout(db, test_user.id)
    assert (await capture_workout_snapshot(db, unknown, explicit_mass=None)).body_mass is None
    await append_workout_weighing(db, test_user.id, client_uuid='later', weight_kg=Decimal(80), measured_on=D(10))
    assert (await capture_workout_snapshot(db, unknown, explicit_mass=None, reconstruct=True)).body_mass is None
    legacy = await workout(db, test_user.id)
    assert (await capture_workout_snapshot(db, legacy, explicit_mass=None, reconstruct=True)).body_mass.source == 'reconstructed'

@pytest.mark.asyncio
async def test_reference_owner_value_and_date(test_user, db):
    row = await workout(db, test_user.id)
    mass_id = await append_workout_weighing(db, test_user.id, client_uuid='owned', weight_kg=Decimal(80), measured_on=D(10))
    for mass in [manual(10, 79, measurement_id=mass_id), manual(11, 80, measurement_id=mass_id),
                 manual(10, 80, measurement_id=mass_id + 999999)]:
        with pytest.raises(HTTPException) as exc:
            await capture_workout_snapshot(db, row, explicit_mass=mass)
        assert exc.value.status_code == 409
    accepted = manual(10, 80, measurement_id=mass_id)
    assert (await capture_workout_snapshot(db, row, explicit_mass=accepted)).body_mass == accepted

@pytest.mark.asyncio
async def test_uuid_scoped_to_owner(test_user, db, p108_seed):
    seeded = await p108_seed()
    a = await append_workout_weighing(db, test_user.id, client_uuid='same', weight_kg=Decimal(80), measured_on=D(10))
    b = await append_workout_weighing(db, seeded.active_owner_id, client_uuid='same', weight_kg=Decimal(90), measured_on=D(10))
    assert a != b
    row = await workout(db, test_user.id)
    with pytest.raises(HTTPException):
        await capture_workout_snapshot(db, row, explicit_mass=manual(10, 90, measurement_id=b))

@pytest.mark.asyncio
async def test_saved_phase_and_source_changes(test_user, db):
    block = TrainingBlock(app_user_id=test_user.id, block_index=1,
                          phases=[{'phase_number': 1, 'name': 'Recovery', 'effort_tier': 'deload', 'length_days': 7}],
                          microcycle_length=7, start_date=D(10), planned_end_date=D(17), status='active')
    db.add(block)
    await db.flush()
    row = await workout(db, test_user.id)
    row.training_block_id = block.id
    row.mesocycle_phase = 1
    result = await capture_workout_snapshot(db, row, explicit_mass=None)
    assert result.is_deload is True
    assert result.phase_source == 'training_block'
    block.phases = [{'phase_number': 1, 'effort_tier': 'hard'}]
    correction = manual()
    assert await capture_workout_snapshot(db, row, explicit_mass=correction) == result.model_copy(update={'body_mass': correction})

@pytest.mark.asyncio
async def test_unknown_phase_never_defaults_from_template(test_user, db, deload_phase_mesocycle):
    row = await workout(db, test_user.id)
    row.mesocycle_phase = 1
    row.app_user_mesocycle_id = deload_phase_mesocycle.id
    result = await capture_workout_snapshot(db, row, explicit_mass=None)
    assert result.is_deload is None and result.phase_source is None

@pytest.mark.asyncio
async def test_proven_calendar_phase_fallback(test_user, db):
    row = await workout(db, test_user.id)
    day = UserCalendarDay(app_user_id=test_user.id, target_date=D(15),
                          actual_workout_session_id=row.id, status='completed', meso_tag='deload')
    db.add(day)
    result = await capture_workout_snapshot(db, row, explicit_mass=None)
    assert result.is_deload is True and result.phase_source == 'calendar'
    unlinked = await workout(db, test_user.id)
    unlinked.calendar_day_id = day.id
    assert (await capture_workout_snapshot(db, unlinked, explicit_mass=None)).is_deload is None
