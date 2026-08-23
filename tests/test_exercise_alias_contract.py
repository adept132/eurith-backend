from __future__ import annotations

import importlib
import importlib.util
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from api.routers import exercises as exercises_router
from api.services import import_service
from api.services.app_user_service import get_current_app_user
from api.services.models import Exercise, ExerciseImportAlias


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows if isinstance(self._rows, list) else [self._rows]

    def scalar_one_or_none(self):
        return self._rows


class _QueuedSession:
    def __init__(self, *results):
        self._results = list(results)
        self.statements = []
        self.commits = 0

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self._results.pop(0))

    async def commit(self):
        self.commits += 1


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

    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert (
        "app_user_id",
        "source",
        "normalized_external_name",
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
    assert 31 in compiled.params.values()
    assert "fitbod" in compiled.params.values()
    assert "  Stra\u00dfe\u00a0Press " in compiled.params.values()
    assert "strasse press" in compiled.params.values()
    assert 99 in compiled.params.values()


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


def test_put_alias_route_is_authenticated() -> None:
    route = next(
        (
            candidate
            for candidate in exercises_router.router.routes
            if candidate.path == "/import-alias" and "PUT" in candidate.methods
        ),
        None,
    )
    assert route is not None, "PUT /exercises/import-alias must be registered"
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
    assert get_current_app_user in dependency_calls


@pytest.mark.asyncio
async def test_put_alias_rejects_unknown_or_other_users_exercise_without_writing(
    monkeypatch,
) -> None:
    schema = _schemas()
    session = _QueuedSession(None)
    writes = []

    async def fake_save_alias(**kwargs):
        writes.append(kwargs)

    monkeypatch.setattr(exercises_router, "save_alias", fake_save_alias, raising=False)
    handler = getattr(exercises_router, "put_exercise_import_alias", None)
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
    assert writes == []
    assert session.commits == 0
    lookup = session.statements[0].compile()
    sql = str(lookup)
    assert "exercises.app_user_id IS NULL" in sql
    assert "exercises.app_user_id" in sql
    assert 22 in lookup.params.values()
    assert 999 in lookup.params.values()


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
        )

    monkeypatch.setattr(exercises_router, "save_alias", fake_save_alias, raising=False)
    handler = getattr(exercises_router, "put_exercise_import_alias", None)
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
