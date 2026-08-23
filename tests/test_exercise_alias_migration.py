from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from api.services import import_service


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260822_09_exercise_import_alias_source.py"
)


def _migration():
    assert MIGRATION_PATH.exists(), "the source-aware alias migration must exist"
    spec = importlib.util.spec_from_file_location("exercise_alias_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Bind:
    def __init__(self):
        self.calls = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append((sql, parameters))
        if sql.lstrip().upper().startswith("SELECT"):
            return _Rows(
                [
                    {"id": 1, "external_name": "  Stra\u00dfe\u00a0Press "},
                    {"id": 2, "external_name": "STRASSE\tPRESS"},
                    {"id": 3, "external_name": "\uff22\uff25\uff2e\uff23\uff28"},
                ]
            )
        return _Rows([])


class _Operations:
    def __init__(self):
        self.bind = _Bind()
        self.calls = []

    def get_bind(self):
        return self.bind

    def add_column(self, table_name, column):
        self.calls.append(("add_column", table_name, column))

    def alter_column(self, table_name, column_name, **kwargs):
        self.calls.append(("alter_column", table_name, column_name, kwargs))

    def create_check_constraint(self, name, table_name, condition):
        self.calls.append(("create_check_constraint", name, table_name, str(condition)))

    def create_unique_constraint(self, name, table_name, columns):
        self.calls.append(("create_unique_constraint", name, table_name, tuple(columns)))

    def drop_constraint(self, name, table_name, **kwargs):
        self.calls.append(("drop_constraint", name, table_name, kwargs))

    def drop_column(self, table_name, column_name):
        self.calls.append(("drop_column", table_name, column_name))


def test_migration_follows_current_head_and_matches_runtime_normalization() -> None:
    migration = _migration()

    assert migration.revision == "20260822_09"
    assert migration.down_revision == "20260822_06"
    for raw_name in (
        "  Bench\tPress ",
        "Stra\u00dfe\u00a0Press",
        "\uff22\uff25\uff2e\uff23\uff28\u3000Press",
        "\u03a3\u0395\u0399\u03a1\u0391\u03a3",
    ):
        assert migration.normalize_external_name(raw_name) == (
            import_service.normalize_external_name(raw_name)
        )


def test_upgrade_backfills_deduplicates_then_adds_source_aware_uniqueness(monkeypatch) -> None:
    migration = _migration()
    operations = _Operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    added_column = next(call for call in operations.calls if call[0] == "add_column")
    assert added_column[1] == "exercise_import_aliases"
    assert added_column[2].name == "normalized_external_name"
    assert isinstance(added_column[2].type, sa.String)
    assert added_column[2].nullable is True

    parameter_batches = [parameters for _, parameters in operations.bind.calls if parameters]
    assert parameter_batches == [
        [
            {"row_id": 1, "normalized_name": "strasse press"},
            {"row_id": 2, "normalized_name": "strasse press"},
            {"row_id": 3, "normalized_name": "bench"},
        ]
    ]

    executed_sql = "\n".join(sql for sql, _ in operations.bind.calls)
    assert "source = 'strong'" in executed_sql
    assert "ROW_NUMBER() OVER" in executed_sql
    assert "PARTITION BY app_user_id, source, normalized_external_name" in executed_sql
    assert "ORDER BY id DESC" in executed_sql

    altered_column = next(call for call in operations.calls if call[0] == "alter_column")
    assert altered_column[1:3] == (
        "exercise_import_aliases",
        "normalized_external_name",
    )
    assert isinstance(altered_column[3]["existing_type"], sa.String)
    assert altered_column[3]["existing_type"].length == 200
    assert altered_column[3]["nullable"] is False
    assert (
        "create_unique_constraint",
        "uq_exercise_import_alias_user_source_name",
        "exercise_import_aliases",
        ("app_user_id", "source", "normalized_external_name"),
    ) in operations.calls
    source_check = next(
        call for call in operations.calls if call[0] == "create_check_constraint"
    )
    for source in ("strong", "hevy", "fitbod", "table", "notes"):
        assert source in source_check[3]


def test_downgrade_removes_only_new_alias_schema(monkeypatch) -> None:
    migration = _migration()
    operations = _Operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert operations.calls == [
        (
            "drop_constraint",
            "uq_exercise_import_alias_user_source_name",
            "exercise_import_aliases",
            {"type_": "unique"},
        ),
        (
            "drop_constraint",
            "ck_exercise_import_alias_source",
            "exercise_import_aliases",
            {"type_": "check"},
        ),
        ("drop_column", "exercise_import_aliases", "normalized_external_name"),
    ]
