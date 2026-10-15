import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from api.services.analytics.state import mark_dirty, read_state
from api.services.models import AnalyticsRecordProjection, AnalyticsState, Exercise, UserCalendarDay

pytestmark = pytest.mark.asyncio


async def test_projection_generation_isolation(db, test_user, p108_seed):
    seeded = await p108_seed()
    first, second = uuid.uuid4(), uuid.uuid4()
    def projection(generation):
        return AnalyticsRecordProjection(app_user_id=test_user.id, exercise_id=seeded.exercise_id,
            gym_scope='unknown', rules_version='p1-08-v1', generation=generation, payload={'events': []})
    db.add_all([projection(first), projection(second)])
    await db.commit()
    async with db.begin_nested():
        db.add(projection(first))
        with pytest.raises(IntegrityError):
            await db.flush()
    assert len((await db.execute(select(AnalyticsRecordProjection).where(
        AnalyticsRecordProjection.app_user_id == test_user.id))).scalars().all()) == 2
    state = await read_state(db, test_user.id)
    assert state.source_revision == 0
    await db.execute(update(Exercise).where(Exercise.id == seeded.exercise_id).values(
        main_muscle_group='back', secondary_muscle_groups=['biceps']))
    assert (await read_state(db, test_user.id)).source_revision == 1
    await db.execute(update(Exercise).where(Exercise.id == seeded.exercise_id).values(
        main_muscle_group='back', secondary_muscle_groups=['biceps']))
    assert (await read_state(db, test_user.id)).source_revision == 1
    # An owner with only an active workout is not invalidated.
    assert await db.scalar(text('SELECT count(*) FROM analytics_states WHERE app_user_id = :id'),
                           {'id': seeded.active_owner_id}) == 0
    await db.commit()


async def test_concurrent_revision_and_transaction_ownership(db, test_user):
    await read_state(db, test_user.id)
    await db.rollback()
    assert await db.scalar(select(AnalyticsState).where(AnalyticsState.app_user_id == test_user.id)) is None
    await db.rollback()
    async def mutation():
        async with SessionLocal() as session:
            revision = await mark_dirty(session, test_user.id)
            await session.commit()
            return revision
    assert sorted(await asyncio.gather(mutation(), mutation())) == [1, 2]
    assert await mark_dirty(db, test_user.id) == 3
    await db.rollback()
    assert (await read_state(db, test_user.id)).source_revision == 2
    await db.rollback()


async def test_ready_invariant_and_retained_active_generation(db, test_user):
    state = await read_state(db, test_user.id)
    generation = uuid.uuid4()
    state.active_generation = generation
    state.active_revision = 0
    state.status = 'ready'
    await db.commit()
    assert await mark_dirty(db, test_user.id) == 1
    state = await read_state(db, test_user.id)
    assert state.status == 'pending'
    assert state.active_generation == generation and state.active_revision == 0
    await db.commit()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(update(AnalyticsState).where(AnalyticsState.app_user_id == test_user.id).values(status='ready'))
    await db.rollback()


async def test_calendar_identity_and_nullable_history(db, test_user, p108_seed):
    seeded = await p108_seed()
    assert seeded.workout.analytics_snapshot is None
    old = UserCalendarDay(app_user_id=test_user.id, target_date=date(2026, 10, 1))
    db.add(old)
    await db.flush()
    old_id = old.id
    old.obligation_uuid = uuid.uuid4()
    old.obligation_origin_date = old.target_date
    old.obligation_snapshot = {'source': 'reconstructed'}
    old.target_date = date(2026, 10, 2)
    await db.flush()
    assert old.id == old_id
    assert old.obligation_origin_date == date(2026, 10, 1)
    await db.commit()
