from __future__ import annotations

import hashlib
import importlib
import importlib.util
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Text, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import NoResultFound

from api.main import app
from api.routers import data_io as data_io_router
from api.routers import exercise_aliases as exercise_aliases_router
from api.services import import_service
from api.services.app_user_service import get_current_app_user
from api.services.models import Exercise, ExerciseImportAlias


class _Result:
    def __init__(self, rows):
        self._rows = rows
        self.scalar_one_calls = 0

    def scalars(self):
        return self

    def all(self):
        return self._rows if isinstance(self._rows, list) else [self._rows]

    def scalar_one_or_none(self):
        return self._rows

    def scalar_one(self):
        self.scalar_one_calls += 1
        if isinstance(self._rows, BaseException):
            raise self._rows
        return self._rows


class _QueuedSession:
    def __init__(self, *results):
        self._results = list(results)
        self.statements = []
        self.execution_options = []
        self.results = []
        self.commits = 0

    async def execute(self, statement, *, execution_options=None):
        self.statements.append(statement)
        self.execution_options.append(execution_options)
        result = _Result(self._results.pop(0))
        self.results.append(result)
        return result

    async def commit(self):
        self.commits += 1


class _RefreshingSession(_QueuedSession):
    async def execute(self, statement, *, execution_options=None):
        result = await super().execute(
            statement,
            execution_options=execution_options,
        )
        if execution_options == {"populate_existing": True}:
            compiled = statement.compile(dialect=postgresql.dialect())
            for attribute in (
                "external_name",
                "normalized_external_name",
                "normalized_external_name_sha256",
                "exercise_id",
            ):
                value = next(
                    parameter
                    for key, parameter in compiled.params.items()
                    if key.startswith(f"{attribute}_m")
                )
                setattr(result._rows, attribute, value)
        return result


def _schemas():
    assert importlib.util.find_spec("api.schemas.exercise_aliases") is not None, (
        "exercise alias request/response schemas must exist"
    )
    return importlib.import_module("api.schemas.exercise_aliases")


def _exercise(exercise_id: int, *, owner_id: int | None = None) -> Exercise:
    return Exercise(
        id=exercise_id,
        name=f"Exercise {exercise_id}",
        category="base",
        main_muscle_group="chest",
        secondary_muscle_groups=[],
        equipment_needed=["barbell"],
        difficulty="intermediate",
        fatigue_tier=2,
        source="custom" if owner_id is not None else "default",
        app_user_id=owner_id,
        image_urls=[],
        image_approx=False,
    )


def test_alias_schema_preserves_raw_name_and_accepts_only_adapter_sources() -> None:
    schema = _schemas()
    raw_name = "\u00a0  Stra\u00dfe\tPress  \n"

    payload = schema.ExerciseAliasUpsertRequest(
        source="hevy",
        external_name=raw_name,
        exercise_id=17,
    )

    assert payload.external_name == raw_name
    assert payload.source == "hevy"
    for source in ("strong", "hevy", "fitbod", "table", "notes"):
        assert schema.ExerciseAliasUpsertRequest(
            source=source,
            external_name="Bench",
            exercise_id=17,
        ).source == source

    with pytest.raises(ValidationError):
        schema.ExerciseAliasUpsertRequest(
            source="other",
            external_name="Bench",
            exercise_id=17,
        )
    with pytest.raises(ValidationError):
        schema.ExerciseAliasUpsertRequest(
            source="strong",
            external_name="\u00a0\t\n",
            exercise_id=17,
        )


def test_alias_schema_accepts_int32_boundary_and_rejects_first_overflow() -> None:
    schema = _schemas()

    assert schema.ExerciseAliasUpsertRequest(
        source="strong",
        external_name="Bench",
        exercise_id=2_147_483_647,
    ).exercise_id == 2_147_483_647

    with pytest.raises(ValidationError):
        schema.ExerciseAliasUpsertRequest(
            source="strong",
            external_name="Bench",
            exercise_id=2_147_483_648,
        )


def test_raw_name_expansion_is_validated_against_the_normalized_storage_contract() -> None:
    schema = _schemas()
    raw_name = "\ufdfa" * 200

    payload = schema.ExerciseAliasUpsertRequest(
        source="notes",
        external_name=raw_name,
        exercise_id=17,
    )
    normalized = import_service.normalize_external_name(payload.external_name)

    assert len(payload.external_name) == 200
    assert len(normalized) == 3_600
    assert len(normalized.encode("utf-8")) == 6_600
    assert import_service.validate_external_name(payload.external_name) == normalized
    assert import_service.validate_external_name("\u00df" * 200) == "ss" * 200


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("  Bench\t  Press\n", "bench press"),
        ("Stra\u00dfe\u00a0Press", "strasse press"),
        ("\uff22\uff25\uff2e\uff23\uff28\u3000Press", "bench press"),
        ("\u03a3\u0395\u0399\u03a1\u0391\u03a3   \u03a0\u0399\u0395\u03a3\u0397", "\u03c3\u03b5\u03b9\u03c1\u03b1\u03c3 \u03c0\u03b9\u03b5\u03c3\u03b7"),
    ],
)
def test_alias_normalization_is_unicode_aware_and_deterministic(
    raw_name: str,
    expected: str,
) -> None:
    normalize = getattr(import_service, "normalize_external_name", None)
    assert normalize is not None, "import aliases need one shared normalizer"

    assert normalize(raw_name) == expected
    assert normalize(expected) == expected


def test_alias_model_has_source_aware_unique_identity_and_valid_sources() -> None:
    table = ExerciseImportAlias.__table__
    assert "normalized_external_name" in table.c
    assert table.c.normalized_external_name.nullable is False
    assert isinstance(table.c.normalized_external_name.type, Text)
    assert "normalized_external_name_sha256" in table.c
    assert table.c.normalized_external_name_sha256.nullable is False
    assert table.c.normalized_external_name_sha256.type.length == 64

    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "app_user_id",
        "source",
        "normalized_external_name_sha256",
    ) in unique_columns

    source_checks = " ".join(
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    )
    for source in ("strong", "hevy", "fitbod", "table", "notes"):
        assert source in source_checks


def test_alias_upsert_compiles_to_one_postgresql_conflict_update() -> None:
    build_upsert = getattr(import_service, "build_alias_upsert", None)
    assert build_upsert is not None, "alias persistence needs an atomic upsert"

    statement = build_upsert(
        app_user_id=31,
        source="fitbod",
        external_name="  Stra\u00dfe\u00a0Press ",
        exercise_id=99,
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "ON CONFLICT ON CONSTRAINT uq_exercise_import_alias_user_source_name" in sql
    assert "external_name = excluded.external_name" in sql
    assert "exercise_id = excluded.exercise_id" in sql
    assert "normalized_external_name" in sql
    assert "normalized_external_name_sha256" in sql
    assert (
        "exercise_import_aliases.normalized_external_name = "
        "excluded.normalized_external_name"
    ) in sql
    assert 31 in compiled.params.values()
    assert "fitbod" in compiled.params.values()
    assert "  Stra\u00dfe\u00a0Press " in compiled.params.values()
    assert "strasse press" in compiled.params.values()
    assert hashlib.sha256(b"strasse press").hexdigest() in compiled.params.values()
    assert 99 in compiled.params.values()


def test_alias_upsert_keeps_maximum_normalized_expansion_off_the_btree_key() -> None:
    raw_name = "\ufdfa" * 200

    statement = import_service.build_alias_upsert(
        app_user_id=31,
        source="notes",
        external_name=raw_name,
        exercise_id=99,
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    normalized = import_service.normalize_external_name(raw_name)

    assert normalized in compiled.params.values()
    assert len(normalized.encode("utf-8")) == 6_600
    assert hashlib.sha256(normalized.encode("utf-8")).hexdigest() in compiled.params.values()


@pytest.mark.asyncio
async def test_save_alias_prevalidates_access_and_refreshes_loaded_identity() -> None:
    exercise = _exercise(99, owner_id=31)
    loaded_alias = ExerciseImportAlias(
        id=8,
        app_user_id=31,
        source="fitbod",
        external_name="Stra\u00dfe Press",
        normalized_external_name="strasse press",
        normalized_external_name_sha256=hashlib.sha256(b"strasse press").hexdigest(),
        exercise_id=exercise.id,
    )
    session = _RefreshingSession(exercise, loaded_alias)

    saved = await import_service.save_alias(
        session=session,
        app_user_id=31,
        source="fitbod",
        external_name="STRASSE\tPRESS",
        exercise_id=exercise.id,
    )

    assert saved is loaded_alias
    assert saved.exercise is exercise
    assert saved.external_name == "STRASSE\tPRESS"
    assert saved.exercise_id == exercise.id
    assert len(session.statements) == 2
    access_sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "exercises.app_user_id IS NULL" in access_sql
    assert "exercises.app_user_id" in access_sql
    assert "FOR SHARE OF exercises" in access_sql
    upsert_sql = str(session.statements[1].compile(dialect=postgresql.dialect()))
    assert "RETURNING" in upsert_sql
    assert session.execution_options == [None, {"populate_existing": True}]
    assert session.results[1].scalar_one_calls == 1


@pytest.mark.asyncio
async def test_save_alias_rejects_inaccessible_exercise_before_upsert() -> None:
    session = _QueuedSession(None)

    with pytest.raises(import_service.ExerciseAliasNotAccessibleError):
        await import_service.save_alias(
            session=session,
            app_user_id=31,
            source="fitbod",
            external_name="Private bench",
            exercise_id=99,
        )

    assert len(session.statements) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exercise_id", [True, 0, 2_147_483_648])
async def test_save_alias_rejects_non_int32_ids_before_any_sql(exercise_id) -> None:
    session = _QueuedSession()

    with pytest.raises(ValueError):
        await import_service.save_alias(
            session=session,
            app_user_id=31,
            source="fitbod",
            external_name="Bench",
            exercise_id=exercise_id,
        )

    assert session.statements == []


@pytest.mark.asyncio
async def test_save_alias_surfaces_digest_collision_without_overwriting() -> None:
    exercise = _exercise(99, owner_id=31)
    session = _QueuedSession(exercise, NoResultFound())

    with pytest.raises(import_service.ExerciseAliasIdentityCollisionError):
        await import_service.save_alias(
            session=session,
            app_user_id=31,
            source="fitbod",
            external_name="Bench",
            exercise_id=exercise.id,
        )

    assert len(session.statements) == 2
    assert session.results[1].scalar_one_calls == 1
    assert session.execution_options[1] == {"populate_existing": True}


@pytest.mark.asyncio
async def test_resolver_scopes_alias_lookup_by_user_and_source_and_normalizes_names() -> None:
    exercise = _exercise(7)
    alias = SimpleNamespace(
        normalized_external_name="strasse press",
        exercise_id=exercise.id,
    )
    session = _QueuedSession([exercise], [alias])

    resolver = import_service.ExerciseResolver(
        session,
        app_user_id=51,
        source="hevy",
    )
    await resolver.load()

    assert await resolver.resolve("  STRASSE\tPRESS ") == exercise.id
    alias_query = session.statements[1].compile()
    sql = str(alias_query)
    assert "exercise_import_aliases.app_user_id" in sql
    assert "exercise_import_aliases.source" in sql
    assert 51 in alias_query.params.values()
    assert "hevy" in alias_query.params.values()


def test_put_alias_route_is_registered_once_at_only_the_authenticated_path() -> None:
    routes = [
        candidate
        for candidate in app.routes
        if "PUT" in getattr(candidate, "methods", set())
        and candidate.path.endswith("/import-alias")
    ]

    assert [route.path for route in routes] == ["/exercises/import-alias"]
    route = routes[0]
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
    assert get_current_app_user in dependency_calls


@pytest.mark.asyncio
async def test_legacy_import_writer_bulk_filters_unknown_foreign_and_overflow_ids() -> None:
    accessible = _exercise(13, owner_id=22)
    saved_alias = ExerciseImportAlias(
        id=9,
        app_user_id=22,
        source="strong",
        external_name="My bench",
        normalized_external_name="my bench",
        normalized_external_name_sha256=hashlib.sha256(b"my bench").hexdigest(),
        exercise_id=accessible.id,
    )
    session = _QueuedSession([accessible], [saved_alias])
    writer = getattr(data_io_router, "_save_mapping_aliases", None)
    assert writer is not None, "legacy import mappings must use the authorized bulk writer"

    saved_count = await writer(
        db=session,
        app_user_id=22,
        mappings={
            "My bench": accessible.id,
            "MY   BENCH": 14,
            "Overflow": 2_147_483_648,
        },
    )

    assert saved_count == 1
    assert len(session.statements) == 2
    access = session.statements[0].compile(dialect=postgresql.dialect())
    assert "exercises.app_user_id IS NULL" in str(access)
    assert "FOR SHARE OF exercises" in str(access)
    assert sorted(next(value for value in access.params.values() if isinstance(value, list))) == [13, 14]
    upsert = session.statements[1].compile(dialect=postgresql.dialect())
    assert "My bench" in upsert.params.values()
    assert "MY   BENCH" not in upsert.params.values()
    assert "Overflow" not in upsert.params.values()
    assert session.execution_options == [None, {"populate_existing": True}]


@pytest.mark.asyncio
async def test_legacy_import_saved_count_preserves_equivalent_mapping_attempts() -> None:
    first = _exercise(13, owner_id=22)
    second = _exercise(15, owner_id=22)
    saved_alias = ExerciseImportAlias(
        id=10,
        app_user_id=22,
        source="strong",
        external_name="MY BENCH",
        normalized_external_name="my bench",
        normalized_external_name_sha256=hashlib.sha256(b"my bench").hexdigest(),
        exercise_id=second.id,
    )
    session = _QueuedSession([first, second], [saved_alias])

    saved_count = await data_io_router._save_mapping_aliases(
        db=session,
        app_user_id=22,
        mappings={"My bench": first.id, "MY BENCH": second.id},
    )

    assert saved_count == 2
    assert len(session.statements) == 2
    upsert = session.statements[1].compile(dialect=postgresql.dialect())
    assert "MY BENCH" in upsert.params.values()
    assert "My bench" not in upsert.params.values()


@pytest.mark.asyncio
async def test_put_alias_rejects_unknown_or_other_users_exercise_without_writing(
    monkeypatch,
) -> None:
    schema = _schemas()
    session = _QueuedSession(None)
    writes = []

    async def fake_save_alias(**kwargs):
        writes.append(kwargs)
        raise import_service.ExerciseAliasNotAccessibleError("exercise not found")

    monkeypatch.setattr(exercise_aliases_router, "save_alias", fake_save_alias)
    handler = getattr(exercise_aliases_router, "put_exercise_import_alias", None)
    assert handler is not None

    with pytest.raises(HTTPException) as error:
        await handler(
            payload=schema.ExerciseAliasUpsertRequest(
                source="notes",
                external_name="Private bench",
                exercise_id=999,
            ),
            db=session,
            app_user=SimpleNamespace(id=22),
        )

    assert error.value.status_code == 404
    assert writes == [
        {
            "session": session,
            "app_user_id": 22,
            "source": "notes",
            "external_name": "Private bench",
            "exercise_id": 999,
        }
    ]
    assert session.commits == 0
    assert session.statements == []


@pytest.mark.asyncio
async def test_put_alias_returns_confirmation_and_commits_atomic_upsert(monkeypatch) -> None:
    schema = _schemas()
    exercise = _exercise(13, owner_id=22)
    session = _QueuedSession(exercise)
    calls = []

    async def fake_save_alias(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            source=kwargs["source"],
            external_name=kwargs["external_name"],
            normalized_external_name="my bench",
            exercise_id=kwargs["exercise_id"],
            exercise=exercise,
        )

    monkeypatch.setattr(exercise_aliases_router, "save_alias", fake_save_alias)
    handler = getattr(exercise_aliases_router, "put_exercise_import_alias", None)
    assert handler is not None

    response = await handler(
        payload=schema.ExerciseAliasUpsertRequest(
            source="table",
            external_name="  My\tBench ",
            exercise_id=exercise.id,
        ),
        db=session,
        app_user=SimpleNamespace(id=22),
    )

    assert calls == [
        {
            "session": session,
            "app_user_id": 22,
            "source": "table",
            "external_name": "  My\tBench ",
            "exercise_id": exercise.id,
        }
    ]
    assert session.commits == 1
    assert response.source == "table"
    assert response.external_name == "  My\tBench "
    assert response.normalized_external_name == "my bench"
    assert response.exercise_id == exercise.id
    assert response.exercise.id == exercise.id
    assert response.exercise.name == exercise.name
