"""Durable, resumable analytics generations; never touches legacy projections.

rebuild_user owns commits and requires a dedicated session with no pending writes.
A separate transaction-scoped advisory lock serializes workers without holding
the state row while reading history. Writers remain free to invalidate a build.
"""
from collections import defaultdict
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AnalyticsState, AnalyticsRecordProjection, WorkoutSession
from .policy import RULES_VERSION, FORMULA_ID
from .repository import load_facts
from .types import AnalyticsQuery
from .strength import record_history
from .snapshots import capture_workout_snapshot
from .state import read_state, mark_dirty
from .config import analytics_enabled


async def _locked_state(db, user_id):
    return (await db.execute(select(AnalyticsState).where(
        AnalyticsState.app_user_id == user_id, AnalyticsState.rules_version == RULES_VERSION
    ).with_for_update().execution_options(populate_existing=True))).scalar_one()


def _result(state, status=None):
    return dict(status=status or state.status, rules_version=RULES_VERSION, formula_id=FORMULA_ID,
        source_revision=state.source_revision, active_revision=state.active_revision,
        generation=str(state.active_generation) if state.active_generation else None,
        **(state.cursor or {}))


async def _publish(db, user_id, generation, revision):
    state = await _locked_state(db, user_id)
    if state.source_revision != revision or state.building_generation != generation:
        state.status = 'pending'
        result = _result(state, 'superseded')
    else:
        state.active_generation, state.active_revision = generation, revision
        state.building_generation = state.building_revision = None
        state.status, state.last_error = 'ready', None
        result = _result(state)
    await db.commit()
    return result


async def rebuild_user(db, user_id: int, *, resume: bool = True,
                       reconstruct_legacy: bool = False) -> dict:
    """Publish one revision, or return superseded for the next worker sweep.

    Explicit apply is allowed with the read feature off. Reconstruction is a
    one-time opt-in before the first publication, never a later body backfill.
    A crash leaves the cursor and old active generation intact. Retrying the
    same generation overwrites deterministic keys instead of appending events.
    """
    if db.new or db.dirty or db.deleted:
        raise ValueError('rebuild requires a dedicated session without pending writes')
    async with AsyncSession(bind=db.bind) as lease:
        acquired = await lease.scalar(text(
            'SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))'),
            {'key': f'analytics-rebuild:{RULES_VERSION}:{user_id}'})
        if not acquired:
            return dict(status='busy', rules_version=RULES_VERSION)
        await read_state(db, user_id)
        state = await _locked_state(db, user_id)
        if state.active_generation is not None and state.active_revision == state.source_revision:
            result = _result(state)
            await db.commit()
            return result
        prior = state.cursor or {}
        reconstructed = prior.get('reconstructed', 0)
        reconstruction_done = prior.get('reconstruction_done', False)
        # Lock workout facts before modifying snapshots. The state lock above
        # is released first to preserve the normal writer's workout -> state order.
        initial = state.active_generation is None
        await db.commit()
        if reconstruct_legacy and initial and not reconstruction_done:
            missing = (await db.execute(select(WorkoutSession).where(
                WorkoutSession.app_user_id == user_id,
                WorkoutSession.status == 'finished', WorkoutSession.analytics_snapshot.is_(None),
                WorkoutSession.started_at.is_not(None)).order_by(WorkoutSession.id).with_for_update())).scalars().all()
            for workout in missing:
                await capture_workout_snapshot(db, workout, explicit_mass=None, reconstruct=True)
            reconstructed += len(missing)
            if missing:
                await mark_dirty(db, user_id)
            state = await _locked_state(db, user_id)
            state.cursor = dict(prior, reconstructed=reconstructed, reconstruction_done=True)
            await db.commit()
            reconstruction_done = True
        state = await _locked_state(db, user_id)
        revision = state.source_revision
        generation = (state.building_generation if resume and state.building_revision == revision
                      else None) or uuid4()
        state.building_generation, state.building_revision = generation, revision
        state.status, state.last_error = 'rebuilding', None
        state.cursor = dict(reconstructed=reconstructed, reconstruction_done=reconstruction_done,
                            projections=0, events=0)
        await db.commit()
        try:
            facts = await load_facts(db, user_id, AnalyticsQuery(period='all'))
            groups = defaultdict(list)
            for row in facts.graph_sets:
                groups[row.exercise_id, 'all'].append(row)
                groups[row.exercise_id, 'unknown' if row.gym_id is None else f'gym:{row.gym_id}'].append(row)
            counts = dict(reconstructed=reconstructed, reconstruction_done=reconstruction_done,
                workouts=len(facts.global_workouts), sets=len(facts.graph_sets),
                missing_snapshots=sum(w['analytics_snapshot'] is None for w in facts.global_workouts),
                missing_body_mass=sum(not (w['analytics_snapshot'] or {}).get('body_mass') for w in facts.global_workouts),
                projections=0, events=0)
            # Delete only this unpublished generation; a worker lease prevents
            # another worker from publishing or deleting it during this attempt.
            await db.execute(delete(AnalyticsRecordProjection).where(
                AnalyticsRecordProjection.app_user_id == user_id,
                AnalyticsRecordProjection.rules_version == RULES_VERSION,
                AnalyticsRecordProjection.generation == generation))
            for (exercise_id, scope), rows in sorted(groups.items()):
                payload = jsonable_encoder(record_history(rows))
                payload.update(rules_version=RULES_VERSION, formula_id=FORMULA_ID,
                               data_revision=revision, gym_scope=scope)
                await db.execute(insert(AnalyticsRecordProjection).values(
                    app_user_id=user_id, exercise_id=exercise_id, gym_scope=scope,
                    rules_version=RULES_VERSION, generation=generation, payload=payload))
                counts['projections'] += 1
                counts['events'] += len(payload['events'])
            state = await _locked_state(db, user_id)
            state.cursor = counts
            await db.commit()
            return await _publish(db, user_id, generation, revision)
        except Exception as exc:
            await db.rollback()
            state = await _locked_state(db, user_id)
            # Store only the exception type: database errors can contain values,
            # credentials or complete historical payloads in their text.
            state.last_error = type(exc).__name__
            await db.commit()
            raise


async def active_records(db, user_id: int, exercise_id: int, gym_scope: str) -> dict | None:
    """One SQL snapshot: never return a generation for an obsolete revision."""
    P, S = AnalyticsRecordProjection, AnalyticsState
    return await db.scalar(select(P.payload).join(S,
        (S.app_user_id == P.app_user_id) & (S.rules_version == P.rules_version)
        & (S.active_generation == P.generation)).where(P.app_user_id == user_id,
        P.exercise_id == exercise_id, P.gym_scope == gym_scope, P.rules_version == RULES_VERSION,
        S.status == 'ready', S.active_revision == S.source_revision))


async def run_pending_analytics_rebuilds(limit: int = 10) -> int:
    """Bounded restart-safe sweep; pending and crashed rebuilding rows are durable."""
    if not analytics_enabled() or limit <= 0:
        return 0
    from app.database import SessionLocal
    async with SessionLocal() as db:
        users = list((await db.scalars(select(AnalyticsState.app_user_id).where(
            AnalyticsState.rules_version == RULES_VERSION,
            AnalyticsState.status.in_(['pending', 'rebuilding'])).order_by(
                AnalyticsState.updated_at, AnalyticsState.app_user_id).limit(limit))).all())
    processed = 0
    for user_id in users:
        try:
            async with SessionLocal() as db:
                result = await rebuild_user(db, user_id)
                processed += result['status'] != 'busy'
        except Exception:
            # Durable state retains a sanitized failure. Other users still run.
            processed += 1
    return processed
