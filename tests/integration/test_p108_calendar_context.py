from datetime import timedelta

import pytest
from sqlalchemy import select

from api.services.models import AnalyticsState, UserCalendarDay, WorkoutSession
from api.services.periodization.repository import ensure_active_block
from api.services.periodization.service import _regenerate_future
from api.services.scheduling_engine import SchedulingEngine
from tests.integration.test_periodization_calendar import _seed, TODAY
from api.services.scheduling_engine import active_calendar_clause
from api.services.volume.repository import attach_session_to_day
from api.routers.calendar import get_calendar_range


@pytest.mark.asyncio
async def test_generation_identity_noop_and_outer_transaction(test_user, db):
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    count = await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date)
    rows = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id))).scalars())
    assert count == len(rows)
    assert all(d.obligation_uuid and d.obligation_origin_date == d.target_date and d.obligation_snapshot for d in rows)
    state = (await db.execute(select(AnalyticsState).where(AnalyticsState.app_user_id == test_user.id))).scalar_one()
    revision = state.source_revision
    assert await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date) == 0
    await db.refresh(state)
    assert state.source_revision == revision


@pytest.mark.asyncio
async def test_same_block_replacement_preserves_obligations(test_user, db):
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date)
    rows = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id))).scalars())
    before = {r.target_date: (r.obligation_uuid, r.obligation_origin_date, r.obligation_snapshot) for r in rows}
    await _regenerate_future(db, test_user.id, block, TODAY)
    after = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id))).scalars())
    assert all(before[r.target_date] == (r.obligation_uuid, r.obligation_origin_date, r.obligation_snapshot) for r in after)
    assert all(r.obligation_uuid for r in after)


@pytest.mark.asyncio
async def test_cancelled_days_are_audit_only_and_attachment_uses_active_day(test_user, db):
    from api.services.scheduling_engine import cancel_future_calendar
    from datetime import datetime, timezone
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date)
    target = TODAY + timedelta(days=1)
    old = (await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id,
        UserCalendarDay.target_date == target))).scalar_one()
    identity = old.obligation_uuid
    await cancel_future_calendar(db, test_user.id, target, today=TODAY, operation='split_launch')
    await SchedulingEngine.generate_block_days(db, test_user.id, block, target, block.planned_end_date)
    visible = await get_calendar_range(TODAY, block.planned_end_date, db, test_user)
    assert len(visible['calendar']) == 14
    stale = WorkoutSession(app_user_id=test_user.id, source='free', status='finished',
        started_at=datetime.combine(target, datetime.min.time(), timezone.utc), calendar_day_id=old.id)
    db.add(stale)
    await db.flush()
    assert await attach_session_to_day(db, test_user.id, stale) is None
    workout = WorkoutSession(app_user_id=test_user.id, source="free", status='finished', started_at=datetime.combine(target, datetime.min.time(), timezone.utc))
    db.add(workout)
    await db.flush()
    attached = await attach_session_to_day(db, test_user.id, workout)
    assert attached.id != old.id
    assert old.obligation_uuid == identity
    assert old.obligation_snapshot['cancellation']['operation'] == 'split_launch'
    await db.commit()


@pytest.mark.asyncio
async def test_regeneration_preserves_intended_session_and_unknown_history(test_user, db):
    from datetime import datetime, timezone
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date)
    rows = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id).order_by(UserCalendarDay.target_date))).scalars())
    protected, unknown = rows[1:3]
    protected_id = protected.id
    workout = WorkoutSession(app_user_id=test_user.id, source="free", status='active', started_at=datetime.now(timezone.utc), calendar_day_id=protected_id)
    db.add(workout)
    unknown.obligation_uuid = unknown.obligation_origin_date = unknown.obligation_snapshot = None
    await db.commit()
    await _regenerate_future(db, test_user.id, block, TODAY)
    await db.refresh(workout)
    assert workout.calendar_day_id == protected_id
    unknown_after = (await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id,
        UserCalendarDay.target_date == unknown.target_date, active_calendar_clause()))).scalar_one()
    assert unknown_after.obligation_uuid is None
    await db.commit()


@pytest.mark.asyncio
async def test_replacement_rest_and_shortened_tail_keep_cancellation_proof(test_user, db):
    from api.services.models import UserSplit
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date)
    split = (await db.execute(select(UserSplit).where(UserSplit.app_user_id == test_user.id))).scalar_one()
    split.selected_plans = {'blackout_weekdays': [(TODAY + timedelta(days=1)).weekday()]}
    block.planned_end_date = TODAY + timedelta(days=4)
    await _regenerate_future(db, test_user.id, block, TODAY)
    audit = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id))).scalars())
    cancelled = [d for d in audit if (d.obligation_snapshot or {}).get('cancellation')]
    assert len(cancelled) == 10  # one blackout plus nine removed tail dates
    assert all(d.obligation_uuid and d.obligation_origin_date == d.target_date for d in cancelled)
    active = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id,
        active_calendar_clause()))).scalars())
    assert len(active) == 5
    assert next(d for d in active if d.target_date == TODAY + timedelta(days=1)).is_blackout
    await db.commit()


@pytest.mark.asyncio
async def test_undo_restores_known_obligation_snapshot(test_user, db, active_block, fresh_exercise):
    from api.services.models import PeriodizationProposal, UserSplit
    from api.services.periodization import params
    from api.services.periodization.service import apply_decision
    from api.services.volume.repository import utc_today
    today = utc_today()
    await SchedulingEngine.generate_block_days(db, test_user.id, active_block, today + timedelta(days=1), active_block.planned_end_date)
    rows = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id))).scalars())
    before = {d.target_date: (d.obligation_uuid, d.obligation_origin_date, d.obligation_snapshot) for d in rows}
    split = (await db.execute(select(UserSplit).where(UserSplit.app_user_id == test_user.id, UserSplit.is_active.is_(True)))).scalar_one()
    split.selected_plans = {'blackout_weekdays': [rows[0].target_date.weekday()]}
    proposal = PeriodizationProposal(app_user_id=test_user.id, block_id=active_block.id,
        kind=params.KIND_GOAL_PLAN, reason_code='pace_behind', status=params.STATUS_PENDING,
        payload={'exercise_id': fresh_exercise.id, 'levers': [{'index': 0, 'kind': 'lift_frequency'}]})
    db.add(proposal)
    await db.commit()
    result = await apply_decision(db, test_user.id, proposal.id, params.ACTION_APPLY_GOAL,
                                  client_uuid='apply', options={'accepted': [0]})
    assert result['applied'] == [0]
    assert all('obligation_snapshot' in r for r in proposal.payload['applied_snapshot']['days'])
    result = await apply_decision(db, test_user.id, proposal.id, params.ACTION_UNDO_GOAL, client_uuid='undo')
    assert result['status'] == 'undone'
    rows = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id,
        active_calendar_clause()))).scalars())
    assert {d.target_date: (d.obligation_uuid, d.obligation_origin_date, d.obligation_snapshot) for d in rows} == before


@pytest.mark.asyncio
async def test_new_backdated_schedule_is_not_proven_history(test_user, db):
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date)
    rows = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id))).scalars())
    assert all(d.obligation_snapshot['reconstructed'] for d in rows)


@pytest.mark.asyncio
async def test_split_launch_cancels_future_once_and_keeps_due_history(test_user, db, monkeypatch):
    from datetime import date
    from types import SimpleNamespace
    from api.routers.splits import launch_split
    from api.services.models import UserSplit
    class OperationDate(date):
        @classmethod
        def today(cls):
            return TODAY + timedelta(days=2)
    monkeypatch.setattr('api.services.scheduling_engine.date', OperationDate)
    monkeypatch.setattr('api.services.volume.repository.utc_today', OperationDate.today)
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date)
    state = (await db.execute(select(AnalyticsState).where(AnalyticsState.app_user_id == test_user.id))).scalar_one()
    revision = state.source_revision
    old_rows = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id))).scalars())
    split = (await db.execute(select(UserSplit).where(UserSplit.app_user_id == test_user.id))).scalar_one()
    await launch_split(SimpleNamespace(blueprint_id=split.blueprint_id, start_date=TODAY, blackout_weekdays=[]), db, test_user)
    await db.refresh(state)
    assert state.source_revision == revision + 1
    assert all(not (d.obligation_snapshot or {}).get('cancellation') for d in old_rows if d.target_date <= OperationDate.today())
    assert all(d.obligation_snapshot.get('cancellation') for d in old_rows if d.target_date > OperationDate.today())
    active = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id, active_calendar_clause()))).scalars())
    assert len({d.target_date for d in active}) == len(active)


@pytest.mark.asyncio
async def test_close_block_cancels_known_future_once(test_user, db):
    from tests.integration.test_periodization_apply import _seed_with_split, _proposal, TODAY as CLOSE_TODAY
    from api.services.periodization import params
    from api.services.periodization.service import apply_decision
    block = await _seed_with_split(db, test_user.id)
    await SchedulingEngine.generate_block_days(db, test_user.id, block, block.start_date, block.planned_end_date)
    old = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id,
        UserCalendarDay.target_date > CLOSE_TODAY))).scalars())
    identities = {d.id: d.obligation_uuid for d in old}
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_EARLY_DELOAD)
    state = (await db.execute(select(AnalyticsState).where(AnalyticsState.app_user_id == test_user.id))).scalar_one()
    revision = state.source_revision
    await apply_decision(db, test_user.id, proposal.id, 'close_block', today=CLOSE_TODAY, client_uuid='close')
    await db.refresh(state)
    assert state.source_revision == revision + 1

    assert all(d.obligation_uuid == identities[d.id] and d.obligation_snapshot['cancellation']['operation'] == 'close_block' for d in old)
    await apply_decision(db, test_user.id, proposal.id, 'close_block', today=CLOSE_TODAY, client_uuid='close')
    await db.refresh(state)
    assert state.source_revision == revision + 1


@pytest.mark.asyncio
async def test_acknowledging_block_boundary_does_not_dirty_calendar(test_user, db):
    from tests.integration.test_periodization_apply import _seed_with_split, _proposal, TODAY as OP_TODAY
    from api.services.periodization import params
    from api.services.periodization.service import apply_decision
    block = await _seed_with_split(db, test_user.id)
    await SchedulingEngine.generate_block_days(db, test_user.id, block, block.start_date, block.planned_end_date)
    proposal = await _proposal(db, test_user.id, block.id, params.KIND_BLOCK_BOUNDARY)
    state = (await db.execute(select(AnalyticsState).where(AnalyticsState.app_user_id == test_user.id))).scalar_one()
    revision = state.source_revision
    await apply_decision(db, test_user.id, proposal.id, 'start_next_block', today=OP_TODAY)
    await db.refresh(state)
    assert state.source_revision == revision


@pytest.mark.asyncio
async def test_cancelled_carriers_excluded_from_budget_notifications_and_export_context(test_user, db, seeded_plan):
    from api.services.models import AppNotification
    from api.services.notification_service import materialize_domain_notifications
    from api.services.scheduling_engine import ensure_obligation_identity, cancel_obligation
    from api.services.csv_export import _load_micro_days
    from api.services.volume.repository import Window, prescribed_for
    target = TODAY + timedelta(days=1)
    cancelled = UserCalendarDay(app_user_id=test_user.id, target_date=target, plan_id=seeded_plan.id,
        microcycle_day_number=99, status='planned', is_rest_day=False, is_blackout=False)
    ensure_obligation_identity(cancelled)
    cancel_obligation(cancelled, operation='split_launch', today=TODAY)
    completed = UserCalendarDay(app_user_id=test_user.id, target_date=TODAY, microcycle_day_number=1,
        status='completed', is_rest_day=False, is_blackout=False, actual_workout_session_id=991)
    db.add_all([cancelled, completed])
    await db.flush()
    window = Window(None, 1, None, target, target, False)
    assert await prescribed_for(db, test_user.id, window) == {}
    assert await _load_micro_days(db, test_user.id) == {TODAY: 1}
    # Audit exclusion is independent of legacy status materialization.
    cancelled.plan_id = None
    cancelled.status = 'planned'
    await db.flush()
    await materialize_domain_notifications(db, test_user.id, target)
    notices = list((await db.execute(select(AppNotification).where(AppNotification.app_user_id == test_user.id,
        AppNotification.event_type == 'training_day_without_plan'))).scalars())
    assert notices == []
    await db.commit()


@pytest.mark.asyncio
async def test_horizon_changes_revision_only_when_dates_are_created(test_user, db):
    from sqlalchemy import delete
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    await SchedulingEngine.generate_block_days(db, test_user.id, block, TODAY, block.planned_end_date)
    state = (await db.execute(select(AnalyticsState).where(AnalyticsState.app_user_id == test_user.id))).scalar_one()
    revision = state.source_revision
    await SchedulingEngine.ensure_horizon(db, test_user.id, TODAY)
    await db.refresh(state)
    assert state.source_revision == revision
    await db.execute(delete(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id,
        UserCalendarDay.target_date == block.planned_end_date))
    await db.commit()
    await SchedulingEngine.ensure_horizon(db, test_user.id, TODAY)
    await db.refresh(state)
    assert state.source_revision == revision + 1


@pytest.mark.asyncio
async def test_stale_start_cannot_link_a_cancelled_obligation(test_user, db, client):
    from api.services.scheduling_engine import ensure_obligation_identity, cancel_obligation
    cancelled = UserCalendarDay(app_user_id=test_user.id, target_date=TODAY + timedelta(days=1),
        status='planned', is_rest_day=False, is_blackout=False)
    ensure_obligation_identity(cancelled)
    cancel_obligation(cancelled, operation='split_launch', today=TODAY)
    db.add(cancelled)
    await db.commit()
    response = await client.post('/workouts/start', json={'source': 'free', 'calendar_day_id': cancelled.id})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_late_undo_does_not_cancel_already_due_generated_obligations(test_user, db, monkeypatch):
    from datetime import date
    from api.services.models import PeriodizationProposal
    from api.services.goal.service import apply_goal_decision, undo_goal_decision
    from api.services.periodization import params
    class OperationDate(date):
        current = TODAY
        @classmethod
        def today(cls):
            return cls.current
    monkeypatch.setattr('api.services.goal.service.date', OperationDate)
    await _seed(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)
    proposal = PeriodizationProposal(app_user_id=test_user.id, block_id=block.id,
        kind=params.KIND_GOAL_PLAN, reason_code='pace_behind', status=params.STATUS_PENDING,
        payload={'levers': [{'index': 0, 'kind': 'lift_frequency'}]})
    db.add(proposal)
    await db.flush()
    await apply_goal_decision(db, test_user.id, proposal, params.ACTION_APPLY_GOAL, {'accepted': [0]})
    await db.commit()

    OperationDate.current = TODAY + timedelta(days=3)
    await undo_goal_decision(db, test_user.id, proposal)
    active = list((await db.execute(select(UserCalendarDay).where(UserCalendarDay.app_user_id == test_user.id,
        active_calendar_clause()))).scalars())
    assert {d.target_date for d in active} == {TODAY + timedelta(days=i) for i in (1, 2, 3)}
    await db.commit()


@pytest.mark.asyncio
async def test_attachment_preserves_legacy_fact_and_records_explicit_vs_inferred_proof(test_user, db):
    from datetime import datetime, timezone
    from api.services.analytics.context import adherence_summary
    from api.services.scheduling_engine import ensure_obligation_identity
    rows, workouts = [], []
    for index in (1, 2):
        day = UserCalendarDay(app_user_id=test_user.id, target_date=TODAY + timedelta(days=index),
            status='planned', is_rest_day=False, is_blackout=False)
        ensure_obligation_identity(day)
        day.obligation_snapshot = dict(day.obligation_snapshot, reconstructed=False)
        db.add(day)
        await db.flush()
        at = datetime.combine(day.target_date, datetime.min.time(), timezone.utc)
        workout = WorkoutSession(app_user_id=test_user.id, source='free', status='finished',
            started_at=at, finished_at=at, calendar_day_id=day.id if index == 2 else None)
        db.add(workout)
        await db.flush()
        assert await attach_session_to_day(db, test_user.id, workout) is day
        assert day.status == 'completed' and day.actual_workout_session_id == workout.id
        assert day.obligation_snapshot['completion']['source'] == ('date_inferred' if index == 1 else 'explicit_calendar_intent')
        rows.append({key: getattr(day, key) for key in ('id', 'target_date', 'status', 'actual_workout_session_id',
            'obligation_uuid', 'obligation_origin_date', 'obligation_snapshot')})
        workouts.append(dict(id=workout.id, status='finished', finished_at=at))
    summary = adherence_summary(rows, workouts, today=TODAY + timedelta(days=3))
    assert summary['planned_days'] == 2 and summary['completed_days'] == 1
    assert summary['unresolved_completion_count'] == 1 and summary['percent'] is None
    await db.commit()
