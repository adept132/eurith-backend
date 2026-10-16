from datetime import date
import uuid

import pytest
from sqlalchemy import delete, select, event
from api.services.models import AppUser, TrainingBlock, PeriodizationProposal
from app.database import engine


@pytest.mark.asyncio
async def test_empty_index_does_not_create_lifecycle(client, db, test_user):
    queries = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if 'training_blocks' in statement.lower():
            queries.append(statement)
    event.listen(engine.sync_engine, 'before_cursor_execute', record)
    try:
        response = await client.get('/periodization/blocks')
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', record)
    assert response.status_code == 200
    assert response.json() == []
    assert len(queries) == 1
    assert queries[0].lstrip().upper().startswith('SELECT')
    assert not (await db.execute(select(TrainingBlock).where(TrainingBlock.app_user_id == test_user.id))).scalars().all()
    assert not (await db.execute(select(PeriodizationProposal).where(PeriodizationProposal.app_user_id == test_user.id))).scalars().all()
    await db.rollback()


@pytest.mark.asyncio
async def test_index_owned_history_retains_unknown_end(client, db, test_user):
    other = AppUser(firebase_uid=f'index-{uuid.uuid4()}', email=f'{uuid.uuid4()}@example.com')
    db.add(other)
    await db.flush()
    other_id = other.id
    rows = [TrainingBlock(app_user_id=owner, block_index=index, status=status,
        start_date=date(2025, 1, 1), planned_end_date=date(2025, 2, 1),
        actual_end_date=end, microcycle_length=7, phases=[])
        for owner, index, status, end in [(test_user.id, 1, 'closed', None),
            (test_user.id, 2, 'closed', date(2025, 2, 2)),
            (test_user.id, 3, 'active', None), (other_id, 1, 'closed', None)]]
    db.add_all(rows)
    await db.commit()
    try:
        response = await client.get('/periodization/blocks')
        assert response.status_code == 200
        data = response.json()
        assert [r['block_index'] for r in data] == [3, 2, 1]
        assert {r['id'] for r in data} == {r.id for r in rows[:3]}
        assert data[2]['status'] == 'closed'
        assert data[2]['actual_end_date'] is None
        assert data[1]['actual_end_date'] == '2025-02-02'
        assert set(data[0]) == {'id', 'block_index', 'status', 'start_date', 'planned_end_date', 'actual_end_date'}
    finally:
        await db.execute(delete(AppUser).where(AppUser.id == other_id))
        await db.commit()


@pytest.mark.asyncio
async def test_index_requires_authentication(client):
    from api.main import app
    from api.services.app_user_service import get_current_app_user, get_current_app_user_allow_pending
    overrides = app.dependency_overrides.copy()
    app.dependency_overrides.pop(get_current_app_user, None)
    app.dependency_overrides.pop(get_current_app_user_allow_pending, None)
    try:
        response = await client.get('/periodization/blocks')
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
