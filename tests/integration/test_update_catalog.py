"""Public catalog gates OTA by installed build, independently of Firebase auth."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

pytestmark = pytest.mark.asyncio
RUNTIME = "a95ae2339aa9a034bcf49d575e9d238a9badb6ba"


@pytest.fixture
def release_factory():
    def make(**changes):
        from api.services.models import AppRelease
        values = dict(id=uuid4(), platform="android", channel="production-direct",
            delivery_method="eas_update", version_code=29, version_name="1.0.0",
            runtime_version=RUNTIME, release_notes={"ru": "Исправление", "en": "Fix"},
            status="published", is_mandatory=False, source_commit="a" * 40,
            ci_run_id="test", idempotency_key=str(uuid4()), eas_update_group_id=str(uuid4()),
            published_at=datetime.now(timezone.utc))
        values.update(changes)
        return AppRelease(**values)
    return make


@pytest.fixture
def query():
    return dict(channel="production-direct", current_version_code=29, runtime_version=RUNTIME)


async def test_catalog_exists_without_authentication_and_unknown_build_has_no_release(client):
    response = await client.get("/app-releases/android/latest", params={
        "channel": "production-direct", "current_version_code": 99123, "runtime_version": RUNTIME})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == dict(current_version_code=99123, update_available=False,
        mandatory=False, current_release_withdrawn=False, release=None)


@pytest_asyncio.fixture
async def stored_release(db, release_factory):
    row = release_factory()
    db.add(row)
    await db.commit()
    yield row
    from api.services.models import AppRelease
    await db.execute(delete(AppRelease).where(AppRelease.ci_run_id == "test"))
    await db.commit()


async def test_exact_ota_is_discovered(client, query, stored_release):
    response = await client.get("/app-releases/android/latest", params=query)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["update_available"] is True
    assert body["mandatory"] is False
    assert body["release"]["id"] == str(stored_release.id)
    assert body["release"]["eas_update_group_id"] == stored_release.eas_update_group_id
    assert body["release"]["download_url"] is None


@pytest.mark.parametrize("mismatch", [
    {"runtime_version": "other"}, {"runtime_version": None},
    {"channel": "production-play"}, {"current_version_code": 28}])
async def test_mismatched_ota_is_not_offered(client, query, stored_release, mismatch):
    params = {**query, **mismatch}
    params = {key: value for key, value in params.items() if value is not None}
    response = await client.get("/app-releases/android/latest", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["update_available"] is False
    assert response.json()["release"] is None


async def test_mandatory_ota_flag_is_returned(client, db, query, stored_release):
    stored_release.is_mandatory = True
    await db.commit()
    response = await client.get("/app-releases/android/latest", params=query)
    assert response.json()["mandatory"] is True


async def test_withdrawn_ota_is_never_returned(client, db, query, stored_release):
    stored_release.status = "withdrawn"
    stored_release.withdrawn_at = datetime.now(timezone.utc)
    stored_release.withdrawal_reason = "test withdrawal"
    await db.commit()
    response = await client.get("/app-releases/android/latest", params=query)
    assert response.json()["release"] is None


async def test_native_priority_and_minimum_version_gate(client, db, query, stored_release, release_factory):
    native = release_factory(delivery_method="direct_apk", version_code=30,
        min_supported_version_code=30, artifact_storage_key="android/sha256/" + "b" * 64 + ".apk",
        artifact_sha256="b" * 64, artifact_size_bytes=100, eas_update_group_id=None)
    db.add(native)
    await db.commit()
    response = await client.get("/app-releases/android/latest", params=query)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["release"]["id"] == str(native.id)
    assert body["mandatory"] is True
    assert body["release"]["download_url"].endswith(f"/app-releases/{native.id}/download")


async def test_withdrawn_installed_native_makes_replacement_mandatory(client, db, query, stored_release, release_factory):
    installed = release_factory(delivery_method="direct_apk", status="withdrawn",
        withdrawn_at=datetime.now(timezone.utc), withdrawal_reason="withdrawn",
        artifact_storage_key="android/sha256/" + "c" * 64 + ".apk",
        artifact_sha256="c" * 64, artifact_size_bytes=100, eas_update_group_id=None)
    db.add(installed)
    await db.commit()
    body = (await client.get("/app-releases/android/latest", params=query)).json()
    assert body["current_release_withdrawn"] is True
    assert body["mandatory"] is True
    assert body["release"]["id"] == str(stored_release.id)


async def test_query_validation_and_no_public_mutation(client):
    response = await client.get("/app-releases/android/latest", params={
        "channel": "unknown", "current_version_code": 0})
    assert response.status_code == 422
    assert (await client.post("/app-releases/android/latest", json={})).status_code == 405

async def test_admin_registration_shares_group_across_builds_and_is_idempotent(db, stored_release):
    from api.schemas.releases import EasCatalogRegistration
    from api.services.release_catalog_admin import register_eas_releases, RegistrationConflict
    from api.services.models import AppRelease
    from sqlalchemy import select
    group = uuid4()
    command = EasCatalogRegistration(version_codes=[28, 29], version_name="1.0.0",
        runtime_version=RUNTIME, eas_update_group_id=group, eas_update_id=uuid4(),
        source_commit="d" * 40, release_notes={"ru": "Исправлено", "en": "Fixed"}, ci_run_id="test")
    rows = await register_eas_releases(db, command)
    await db.commit()
    assert [row.version_code for row in rows] == [28, 29]
    assert len({row.eas_update_group_id for row in rows}) == 1
    ids = [row.id for row in rows]
    retry = await register_eas_releases(db, command)
    await db.commit()
    assert [row.id for row in retry] == ids
    with pytest.raises(RegistrationConflict):
        await register_eas_releases(db, command.model_copy(update={"source_commit": "e" * 40}))
    persisted = (await db.execute(select(AppRelease).where(AppRelease.id.in_(ids)))).scalars().all()
    assert {row.source_commit for row in persisted} == {"d" * 40}
    rows[1].status = "withdrawn"
    rows[1].withdrawn_at = datetime.now(timezone.utc)
    rows[1].withdrawal_reason = "withdrawn"
    await db.commit()
    retry = await register_eas_releases(db, command)
    await db.commit()
    assert retry[1].status == "withdrawn"


async def test_admin_conflict_rolls_back_entire_batch(db, stored_release):
    from api.schemas.releases import EasCatalogRegistration
    from api.services.release_catalog_admin import register_eas_releases, RegistrationConflict
    from api.services.models import AppRelease
    from sqlalchemy import select
    group = uuid4()
    one = EasCatalogRegistration(version_codes=[29], version_name="1.0.0", runtime_version=RUNTIME,
        eas_update_group_id=group, source_commit="d" * 40,
        release_notes={"ru": "Исправлено", "en": "Fixed"}, ci_run_id="test")
    await register_eas_releases(db, one)
    await db.commit()
    with pytest.raises(RegistrationConflict):
        await register_eas_releases(db, one.model_copy(update={"version_codes": [28, 29], "source_commit": "e" * 40}))
    rows = (await db.execute(select(AppRelease).where(AppRelease.eas_update_group_id == str(group)))).scalars().all()
    assert [row.version_code for row in rows] == [29]


@pytest.mark.parametrize("changes", [
    {"version_codes": [0]}, {"version_codes": [28, 28]}, {"eas_update_group_id": "invalid"},
    {"source_commit": "bad"}, {"runtime_version": " "}, {"channel": "invalid"},
    {"release_notes": {"ru": "", "en": "Fix"}}, {"min_supported_version_code": 30}])
async def test_admin_command_rejects_invalid_metadata(changes):
    from pydantic import ValidationError
    from api.schemas.releases import EasCatalogRegistration
    values = dict(version_codes=[28, 29], version_name="1.0.0", runtime_version=RUNTIME,
        eas_update_group_id=uuid4(), source_commit="d" * 40,
        release_notes={"ru": "Исправлено", "en": "Fixed"}, ci_run_id="test")
    with pytest.raises(ValidationError):
        EasCatalogRegistration(**{**values, **changes})


async def test_additive_catalog_migration_upgrade_downgrade_preserves_existing_tables(db):
    import importlib
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from pathlib import Path
    from api.services.models import AppRelease
    migration = importlib.import_module("migrations.versions.20261007_01_app_release_catalog")
    assert migration.down_revision == "20261002_01"
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == ["20261007_01"]
    schema = "catalog_check_" + uuid4().hex

    def verify(connection):
        savepoint = connection.begin_nested()
        try:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            for table in ["workout_sessions", "body_progress_photos", "gym_profiles"]:
                connection.execute(sa.text(f'CREATE TABLE "{table}" (id integer PRIMARY KEY, marker text)'))
                connection.execute(sa.text(f"INSERT INTO {table} VALUES (1, 'preserved')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                inspector = sa.inspect(connection)
                names = {item["name"] for item in inspector.get_unique_constraints("app_releases", schema=schema)}
                assert "uq_app_releases_eas_update_group_id" in names
                group_constraint = next(item for item in inspector.get_unique_constraints("app_releases", schema=schema)
                    if item["name"] == "uq_app_releases_eas_update_group_id")
                assert group_constraint["column_names"] == ["channel", "version_code", "eas_update_group_id"]
                assert {column["name"] for column in inspector.get_columns("app_releases", schema=schema)} == set(AppRelease.__table__.c.keys())
                migration.downgrade()
                assert set(sa.inspect(connection).get_table_names(schema=schema)) == {
                    "workout_sessions", "body_progress_photos", "gym_profiles"}
                for table in ["workout_sessions", "body_progress_photos", "gym_profiles"]:
                    assert connection.execute(sa.text(f"SELECT marker FROM {table}")).scalar_one() == "preserved"
                migration.upgrade()
                assert "app_releases" in sa.inspect(connection).get_table_names(schema=schema)
        finally:
            savepoint.rollback()
    await (await db.connection()).run_sync(verify)