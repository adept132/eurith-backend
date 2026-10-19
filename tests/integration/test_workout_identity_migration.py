"""Identity migration against the partial index actually present on production."""
import importlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


@pytest.mark.parametrize('existing', ['absent', 'legacy_index', 'legacy_constraint', 'wrong_index', 'duplicates'])
async def test_identity_migration_preserves_existing_protection_and_history(db, existing):
    migration=importlib.import_module('migrations.versions.20260827_01_workout_session_client_uuid_unique')
    schema='identity_check_'+uuid4().hex

    def verify(connection):
        savepoint=connection.begin_nested()
        try:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(sa.text('CREATE TABLE workout_sessions (id integer PRIMARY KEY, app_user_id bigint NOT NULL, client_uuid uuid, marker text)'))
            connection.execute(sa.text("INSERT INTO workout_sessions VALUES (1, 1, NULL, 'preserved')"))
            if existing=='legacy_index':
                connection.execute(sa.text('CREATE UNIQUE INDEX uq_workout_sessions_user_client_uuid ON workout_sessions(app_user_id,client_uuid) WHERE client_uuid IS NOT NULL'))
            elif existing=='legacy_constraint':
                connection.execute(sa.text('ALTER TABLE workout_sessions ADD CONSTRAINT uq_workout_sessions_user_client_uuid UNIQUE(app_user_id,client_uuid)'))
            elif existing=='wrong_index':
                connection.execute(sa.text('CREATE INDEX uq_workout_sessions_user_client_uuid ON workout_sessions(client_uuid)'))
            elif existing=='duplicates':
                connection.execute(sa.text("INSERT INTO workout_sessions VALUES (2,1,'00000000-0000-4000-8000-000000000001','duplicate'),(3,1,'00000000-0000-4000-8000-000000000001','duplicate')"))
            original_index=connection.scalar(sa.text("SELECT to_regclass('uq_workout_sessions_user_client_uuid')::oid"))
            with Operations.context(MigrationContext.configure(connection)):
                if existing in ('wrong_index','duplicates'):
                    with pytest.raises(RuntimeError,match='identity|identities'):
                        migration.upgrade()
                    return
                migration.upgrade()
                assert connection.scalar(sa.text('SELECT marker FROM workout_sessions WHERE id=1'))=='preserved'
                indexes=sa.inspect(connection).get_indexes('workout_sessions',schema=schema)
                assert any(i['unique'] and i['column_names']==['app_user_id','client_uuid'] for i in indexes)
                if original_index:
                    assert connection.scalar(sa.text("SELECT to_regclass('uq_workout_sessions_user_client_uuid')::oid"))==original_index
                migration.downgrade()
                after_index=connection.scalar(sa.text("SELECT to_regclass('uq_workout_sessions_user_client_uuid')::oid"))
                assert after_index==original_index
                assert connection.scalar(sa.text('SELECT marker FROM workout_sessions WHERE id=1'))=='preserved'
                migration.upgrade()
        finally:
            savepoint.rollback()
    await (await db.connection()).run_sync(verify)
