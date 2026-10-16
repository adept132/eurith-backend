from datetime import date, datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy import event, update
from api.services.models import AnalyticsState, TrainingBlock, Exercise, WorkoutSession
from app.database import engine, SessionLocal

BASE = '/progress/analytics'
pytestmark = pytest.mark.asyncio

async def test_compact_is_opt_in_owned_and_advertised(client, ready, p108_seed, test_user):
    seed = await p108_seed()
    paths = [(f'/exercises/{seed.exercise_id}?period=all', 'exercise'),
             ('/muscles?period=all', 'muscles'),
             (f'/blocks/{ready[0].id}?', 'block'),
             (f'/blocks/compare?block_a_id={ready[0].id}&block_b_id={ready[1].id}', 'comparison')]
    assert (await client.get(BASE+'/capabilities')).json()['read_encodings'] == ['compact-v1']
    for path, kind in paths:
        full = await client.get(BASE+path)
        compact = await client.get(BASE+path+'&encoding=compact-v1')
        assert full.status_code == compact.status_code == 200
        assert 'encoding' not in full.json()
        data = compact.json()
        assert data['encoding'] == 'compact-v1' and data['resource'] == kind
        assert data['owner_uid'] == test_user.firebase_uid
        assert data['payload']['state'] == 'ready' and data['data_revision'] == 3
        assert compact.headers['cache-control'] == 'private, no-store'

@pytest.fixture
async def ready(db, test_user, monkeypatch):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED', 'true')
    db.add(AnalyticsState(app_user_id=test_user.id, rules_version='p1-08-v1', source_revision=3,
                         active_revision=3, active_generation=uuid4(), status='ready'))
    blocks = [TrainingBlock(app_user_id=test_user.id, block_index=i, microcycle_length=7,
              start_date=date(2026, 8+i, 1), planned_end_date=date(2026, 8+i, 7), actual_end_date=date(2026, 8+i, 7), status='closed') for i in range(2)]
    db.add_all(blocks)
    await db.commit()
    return blocks

async def test_compare_route_not_captured_by_id(client, ready):
    response = await client.get(BASE+'/blocks/compare', params=dict(block_a_id=ready[0].id, block_b_id=ready[1].id))
    assert response.status_code == 200
    data = response.json()
    assert data['state'] == data['a']['state'] == data['b']['state'] == 'ready'
    assert data['data_revision'] == data['a']['data_revision'] == data['b']['data_revision'] == 3

async def test_owned_objects_and_empty_selection(client, ready, fresh_exercise, db, p108_seed):
    empty = await client.get(f'{BASE}/exercises/{fresh_exercise.id}')
    assert empty.status_code == 200
    assert empty.json()['summary']['best_e1rm']['reason'] == 'no_data'
    seed = await p108_seed()
    foreign = Exercise(name=str(uuid4()), category='base', main_muscle_group='chest', difficulty='beginner',
                       equipment_needed=[], source='custom', app_user_id=seed.active_owner_id)
    block = TrainingBlock(app_user_id=seed.active_owner_id, block_index=0, microcycle_length=7,
                          start_date=date(2026,8,1), planned_end_date=date(2026,8,7), actual_end_date=date(2026,8,7), status='closed')
    preview = WorkoutSession(app_user_id=seed.active_owner_id, source='free', status='active', started_at=datetime.now(timezone.utc))
    db.add_all([foreign, block, preview]); await db.commit()
    for path in (f'/exercises/{foreign.id}', f'/blocks/{block.id}',
                 f'/blocks/compare?block_a_id={ready[0].id}&block_b_id={block.id}',
                 f'/exercises/{fresh_exercise.id}?preview_workout_id={preview.id}'):
        assert (await client.get(BASE+path)).status_code == 404

@pytest.mark.parametrize('query', ['period=custom&start=2026-09-02&end=2026-09-01', 'period=90d&block_id=1',
 'period=custom', 'period=garbage', 'gym_id=bad', 'period=7d&start=2026-01-01', 'unexpected=1', 'period=7d&period=30d',
 'period=custom&start=0001-01-01&end=0001-01-01', 'period=custom&start=9999-12-30&end=9999-12-30'])
async def test_strict_query(client, ready, query):
    response = await client.get(BASE+'/muscles?'+query)
    assert response.status_code == 422

async def test_context_and_null_reasons(client, ready, p108_seed, db):
    seed = await p108_seed()
    response = await client.get(f'{BASE}/exercises/{seed.exercise_id}?period=all')
    assert response.status_code == 200
    data=response.json()
    assert data['scope']['exercise_id'] == seed.exercise_id
    assert data['summary']['effort']['mean_rir']['value'] is None
    assert data['points'][0]['details']['observations'][0]['set_id']
    ready[0].actual_end_date=None; await db.commit()
    data=(await client.get(f'{BASE}/blocks/compare?block_a_id={ready[0].id}&block_b_id={ready[1].id}')).json()
    assert data['a']['scope']['period'] is None
    assert data['a']['scope']['reason'] == 'unknown_block_end'
    assert data['a']['volume']['calendar_days'] is None
    assert data['b']['volume']['calendar_days'] == 7

async def test_capabilities_and_unavailable(client, ready, monkeypatch, db, test_user):
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','false')
    data=(await client.get(BASE+'/capabilities')).json()
    assert data['write_enabled'] is False and data['state']=='disabled' and data['context_schema_version']==1
    response=await client.get(BASE+'/muscles')
    assert response.json()['state']=='disabled' and 'volume' not in response.json()
    assert response.headers['cache-control']=='private, no-store'
    monkeypatch.setenv('P108_ANALYTICS_ENABLED','true')
    await db.execute(update(AnalyticsState).where(AnalyticsState.app_user_id==test_user.id).values(source_revision=4,status='pending'))
    await db.commit()
    data=(await client.get(BASE+'/capabilities')).json()
    assert data['write_enabled'] is True and data['state']=='rebuilding'
    data=(await client.get(BASE+'/muscles')).json()
    assert data['state']=='rebuilding' and 'volume' not in data

async def test_capabilities_profile_timezone_is_owned_and_defaults_to_utc(client, ready, db, test_user, p108_seed):
    from api.services.models import AppUserProfile
    seed = await p108_seed()
    db.add(AppUserProfile(app_user_id=seed.active_owner_id, timezone='America/Los_Angeles'))
    await db.commit()
    assert (await client.get(BASE+'/capabilities')).json()['timezone'] == 'UTC'
    db.add(AppUserProfile(app_user_id=test_user.id, timezone='Europe/Moscow'))
    await db.commit()
    response = await client.get(BASE+'/capabilities')
    assert response.status_code == 200
    assert response.json()['timezone'] == 'Europe/Moscow'
    assert response.headers['cache-control'] == 'private, no-store'

@pytest.mark.parametrize('phase', ['early', 'late'])
async def test_read_during_generation_change(client, ready, test_user, monkeypatch, phase):
    from api.services.analytics import repository, service
    original = repository._rows if phase=='early' else service.muscle_analytics
    changed=False
    async def racing(*args, **kwargs):
        nonlocal changed
        result=await original(*args, **kwargs)
        if not changed:
            changed=True
            async with SessionLocal() as other:
                await other.execute(update(AnalyticsState).where(AnalyticsState.app_user_id==test_user.id).values(
                    source_revision=4, active_revision=4, active_generation=uuid4(),status='ready'))
                await other.commit()
        return result
    monkeypatch.setattr(repository if phase=='early' else service, '_rows' if phase=='early' else 'muscle_analytics', racing)
    data=(await client.get(BASE+'/muscles')).json()
    assert data['state']=='rebuilding' and 'volume' not in data

async def test_integrated_query_budgets(client, ready, fresh_exercise, db, test_user):
    preview=WorkoutSession(app_user_id=test_user.id,source='free',status='active',started_at=datetime.now(timezone.utc))
    db.add(preview); await db.commit()
    # Only external token verification is replaced; all app-user/profile/account work runs.
    from api.main import app
    from api.deps import get_current_firebase_claims
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_firebase_claims]=lambda: {'uid':test_user.firebase_uid, 'email':test_user.email}
    counts=[]
    for path, limit in [(f'/exercises/{fresh_exercise.id}',12),
      (f'/exercises/{fresh_exercise.id}?preview_workout_id={preview.id}',12),
      (f'/blocks/compare?block_a_id={ready[0].id}&block_b_id={ready[1].id}',20)]:
        statements=[]
        def capture(conn,cursor,statement,parameters,context,executemany):
            if statement.lstrip().upper().startswith('SELECT'): statements.append(statement)
        event.listen(engine.sync_engine,'before_cursor_execute',capture)
        try: response=await client.get(BASE+path)
        finally: event.remove(engine.sync_engine,'before_cursor_execute',capture)
        assert response.status_code==200 and response.json()['state']=='ready'
        counts.append((len(statements),limit))
    print('Full authenticated SELECT counts:',counts)
    assert all(count<=limit for count,limit in counts), counts

async def test_extreme_custom_dates_repository(db, test_user):
    from api.services.analytics.repository import load_facts
    from api.services.analytics.types import AnalyticsQuery
    for day in (date.min, date(9999,12,30)):
        with pytest.raises(ValueError, match='invalid_period_bounds'):
            await load_facts(db,test_user.id,AnalyticsQuery(period='custom',start=day,end=day))


async def test_finished_preview_rejected(client, ready, p108_seed):
    seed=await p108_seed()
    response=await client.get(f'{BASE}/exercises/{seed.exercise_id}?preview_workout_id={seed.workout.id}')
    assert response.status_code==422 and response.json()['detail']=='preview_workout_not_active'

@pytest.mark.parametrize('day', ['0001-01-08', '9999-12-23'])
async def test_representable_edge_dates(client, ready, day):
    response=await client.get(f'{BASE}/muscles?period=custom&start={day}&end={day}')
    assert response.status_code==200
    assert response.json()['scope']['period']['start']==day

async def test_initial_fence_precedes_first_fact(client, ready, test_user, monkeypatch):
    from api.services.analytics import service
    original=service.load_facts
    async def racing(*args, **kwargs):
        async with SessionLocal() as other:
            await other.execute(update(AnalyticsState).where(AnalyticsState.app_user_id==test_user.id).values(
                source_revision=4, active_revision=4, active_generation=uuid4(),status='ready'))
            await other.commit()
        return await original(*args, **kwargs)
    monkeypatch.setattr(service,'load_facts',racing)
    data=(await client.get(BASE+'/muscles')).json()
    assert data['state']=='rebuilding' and 'volume' not in data

@pytest.mark.parametrize('target', ['b', 'exercise', 'baseline'])
async def test_nested_revision_fence(client, ready, p108_seed, db, monkeypatch, target):
    from api.services.analytics import service
    seed=await p108_seed(finished_at=datetime(2026,9,3,tzinfo=timezone.utc))
    original=service.compare_blocks
    async def mismatched(*args, **kwargs):
        result=await original(*args, **kwargs)
        if target=='b': result.b.data_revision=4
        elif target=='exercise': result.b.exercises[seed.exercise_id].data_revision=4
        else: result.b.exercises[seed.exercise_id].record_baseline.data_revision=4
        return result
    monkeypatch.setattr(service,'compare_blocks',mismatched)
    data=(await client.get(f'{BASE}/blocks/compare?block_a_id={ready[0].id}&block_b_id={ready[1].id}')).json()
    assert data['state']=='rebuilding' and 'a' not in data and 'b' not in data

async def test_authentication_required():
    from httpx import ASGITransport, AsyncClient
    from api.main import app
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
        response=await client.get(BASE+'/capabilities')
    assert response.status_code in (401,403)

@pytest.mark.parametrize('path', ['/blocks/compare?block_a_id=1&block_b_id=2&period=7d','/blocks/1?start=2026-01-01'])
async def test_block_ranges_cannot_be_overridden(client,ready,path):
    assert (await client.get(BASE+path)).status_code==422

async def test_measurement_window_timezone_underflow(client, ready, db, test_user):
    from api.services.models import AppUserProfile
    db.add(AppUserProfile(app_user_id=test_user.id, timezone='Europe/Moscow')); await db.commit()
    response=await client.get(BASE+'/muscles?period=custom&start=0001-01-08&end=0001-01-08')
    assert response.status_code==422 and response.json()['detail']=='invalid_period_bounds'
