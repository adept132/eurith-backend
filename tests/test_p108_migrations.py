"""Real PostgreSQL upgrade, independently of integration's init_db autouse fixture."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import MetaData, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


ROOT = Path(__file__).resolve().parents[1]
ADDITIONS = {
    'workout_sessions': {'analytics_snapshot'},
    'workout_session_exercises': {'analytics_load_defaults'},
    'workout_session_sets': {'analytics_load_snapshot', 'anomaly_review'},
    'user_calendar': {'obligation_uuid', 'obligation_origin_date', 'obligation_snapshot'},
}


def guarded_url():
    url = make_url(os.environ['TEST_DATABASE_URL'])
    assert url.host in {'localhost', '127.0.0.1', '::1'}
    assert url.database.startswith('eurith_p108_test')
    return url.set(database='eurith_p108_test_migrations')


def command(url, *args):
    env = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False),
               TEST_DATABASE_URL=url.render_as_string(hide_password=False))
    result = subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=ROOT,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def old_schema(url):
    """Portable pre-feature metadata: exclude only this upgrade's additive objects.

    Unlike integration init_db, this creates no feature columns/tables/indexes.
    All unrelated production metadata remains present for subsequent init_db.
    """
    from api.services.models import Base
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name not in {'analytics_states', 'analytics_record_projections'}:
            table.to_metadata(metadata)
    for name, columns in ADDITIONS.items():
        table = metadata.tables[name]
        for index in list(table.indexes):
            if any(column.name in columns for column in index.columns):
                table.indexes.remove(index)
        for name in columns:
            table._columns.remove(table.c[name])
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        # Guard immediately before the only destructive reset.
        assert engine.url.host in {'localhost', '127.0.0.1', '::1'}
        assert engine.url.database == 'eurith_p108_test_migrations'
        await connection.execute(text('DROP SCHEMA public CASCADE'))
        await connection.execute(text('CREATE SCHEMA public'))
        await connection.run_sync(metadata.create_all)
        await connection.execute(metadata.tables['app_users'].insert().values(id=81001, firebase_uid='p108-old', email='p108-old@example.com'))
        await connection.execute(metadata.tables['workout_sessions'].insert().values(id=81001, app_user_id=81001, source='free', status='finished'))
        assert await connection.scalar(text("SELECT to_regclass('analytics_states')")) is None
    await engine.dispose()


async def verify(url):
    from api.services.models import Exercise, WorkoutSessionExercise
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        assert await connection.scalar(text('SELECT analytics_snapshot IS NULL FROM workout_sessions WHERE id=81001'))
        assert await connection.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname='invalidate_analytics_muscle_mapping' AND NOT tgisinternal")) == 1
        assert await connection.scalar(text("SELECT data_type FROM information_schema.columns WHERE table_name='workout_session_exercises' AND column_name='exercise_id'")) == 'integer'
        await connection.execute(Exercise.__table__.insert().values(id=81001, name='p108 old exercise', category='base', main_muscle_group='chest', secondary_muscle_groups=[], equipment_needed=[], difficulty='beginner'))
        await connection.execute(WorkoutSessionExercise.__table__.insert().values(workout_session_id=81001, exercise_id=81001, order_index=0))
        await connection.execute(text("UPDATE exercises SET main_muscle_group='back', secondary_muscle_groups='[\"biceps\"]' WHERE id=81001"))
        assert await connection.scalar(text('SELECT source_revision FROM analytics_states WHERE app_user_id=81001')) == 1
        await connection.execute(text("UPDATE exercises SET main_muscle_group='back' WHERE id=81001"))
        assert await connection.scalar(text('SELECT source_revision FROM analytics_states WHERE app_user_id=81001')) == 1
    await engine.dispose()


def test_schema_upgrade_and_init_db():
    url = guarded_url()
    asyncio.run(old_schema(url))
    command(url, 'stamp', '20261009_03')
    command(url, 'upgrade', 'head')
    command(url, 'upgrade', 'head')
    asyncio.run(verify(url))
    env = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False))
    result = subprocess.run([sys.executable, '-c',
        'import asyncio\nfrom app.database import init_db, engine\n'
        'async def main():\n await init_db()\n await engine.dispose()\n'
        'asyncio.run(main())'], cwd=ROOT, env=env,
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    command(url, 'downgrade', '20261009_03')
    command(url, 'upgrade', 'head')
