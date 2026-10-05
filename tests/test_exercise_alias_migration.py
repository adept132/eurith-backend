from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.services import import_service
from api.services.exercise_alias_identity import NORMALIZED_EXTERNAL_NAME_MAX_CHARS


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260926_03_exercise_aliases.py"
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
    def __init__(self, select_rows=None):
        self.calls = []
        self.select_rows = select_rows

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append((sql, parameters))
        if sql.lstrip().upper().startswith("SELECT"):
            if self.select_rows is not None:
                return _Rows(self.select_rows)
            return _Rows(
                [
                    {"id": 1, "external_name": "  Stra\u00dfe\u00a0Press "},
                    {"id": 2, "external_name": "STRASSE\tPRESS"},
                    {"id": 3, "external_name": "\uff22\uff25\uff2e\uff23\uff28"},
                    {"id": 4, "external_name": "\u00df" * 200},
                    {"id": 5, "external_name": "ss" * 100},
                    {"id": 6, "external_name": "\ufdfa" * 200},
                ]
            )
        return _Rows([])


class _Operations:
    def __init__(self, bind=None):
        self.bind = bind or _Bind()
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

    assert migration.revision == "20260926_03"
    assert migration.down_revision == "20260926_02"
    assert (
        migration.NORMALIZED_EXTERNAL_NAME_MAX_CHARS
        == NORMALIZED_EXTERNAL_NAME_MAX_CHARS
        == 3_600
    )
    for raw_name in (
        "  Bench\tPress ",
        "Stra\u00dfe\u00a0Press",
        "\uff22\uff25\uff2e\uff23\uff28\u3000Press",
        "\u03a3\u0395\u0399\u03a1\u0391\u03a3",
    ):
        assert migration.normalize_external_name(raw_name) == (
            import_service.normalize_external_name(raw_name)
        )
        assert migration.normalized_external_name_sha256(raw_name) == hashlib.sha256(
            import_service.normalize_external_name(raw_name).encode("utf-8")
        ).hexdigest()


def test_upgrade_backfills_deduplicates_then_adds_source_aware_uniqueness(monkeypatch) -> None:
    migration = _migration()
    operations = _Operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    added_columns = [call for call in operations.calls if call[0] == "add_column"]
    assert [call[2].name for call in added_columns] == [
        "normalized_external_name",
        "normalized_external_name_sha256",
    ]
    assert all(call[1] == "exercise_import_aliases" for call in added_columns)
    assert isinstance(added_columns[0][2].type, sa.Text)
    assert added_columns[0][2].nullable is True
    assert isinstance(added_columns[1][2].type, sa.String)
    assert added_columns[1][2].type.length == 64
    assert added_columns[1][2].nullable is True

    parameter_batches = [parameters for _, parameters in operations.bind.calls if parameters]
    expanded_eszett = "ss" * 200
    expanded_ligature = migration.normalize_external_name("\ufdfa" * 200)
    assert parameter_batches == [
        [
            {
                "row_id": 1,
                "normalized_name": "strasse press",
                "normalized_name_sha256": hashlib.sha256(b"strasse press").hexdigest(),
            },
            {
                "row_id": 2,
                "normalized_name": "strasse press",
                "normalized_name_sha256": hashlib.sha256(b"strasse press").hexdigest(),
            },
            {
                "row_id": 3,
                "normalized_name": "bench",
                "normalized_name_sha256": hashlib.sha256(b"bench").hexdigest(),
            },
            {
                "row_id": 4,
                "normalized_name": expanded_eszett,
                "normalized_name_sha256": hashlib.sha256(
                    expanded_eszett.encode("utf-8")
                ).hexdigest(),
            },
            {
                "row_id": 5,
                "normalized_name": "ss" * 100,
                "normalized_name_sha256": hashlib.sha256(("ss" * 100).encode()).hexdigest(),
            },
            {
                "row_id": 6,
                "normalized_name": expanded_ligature,
                "normalized_name_sha256": hashlib.sha256(
                    expanded_ligature.encode("utf-8")
                ).hexdigest(),
            },
        ]
    ]
    assert len(parameter_batches[0][-1]["normalized_name"]) == 3_600
    assert len(parameter_batches[0][-1]["normalized_name"].encode("utf-8")) == 6_600

    executed_sql = "\n".join(sql for sql, _ in operations.bind.calls)
    assert operations.bind.calls[0][0].strip() == (
        "LOCK TABLE exercise_import_aliases IN SHARE ROW EXCLUSIVE MODE"
    )
    assert "source = 'strong'" in executed_sql
    assert "normalized_external_name_sha256 = :normalized_name_sha256" in executed_sql
    assert "ROW_NUMBER() OVER" in executed_sql
    assert "PARTITION BY app_user_id, source, normalized_external_name" in executed_sql
    assert "ORDER BY id DESC" in executed_sql

    altered_columns = [call for call in operations.calls if call[0] == "alter_column"]
    assert [(call[1], call[2]) for call in altered_columns] == [
        ("exercise_import_aliases", "normalized_external_name"),
        ("exercise_import_aliases", "normalized_external_name_sha256"),
    ]
    assert isinstance(altered_columns[0][3]["existing_type"], sa.Text)
    assert altered_columns[0][3]["nullable"] is False
    assert isinstance(altered_columns[1][3]["existing_type"], sa.String)
    assert altered_columns[1][3]["existing_type"].length == 64
    assert altered_columns[1][3]["nullable"] is False
    assert (
        "create_unique_constraint",
        "uq_exercise_import_alias_user_source_name",
        "exercise_import_aliases",
        ("app_user_id", "source", "normalized_external_name_sha256"),
    ) in operations.calls
    checks = {
        call[1]: call[3]
        for call in operations.calls
        if call[0] == "create_check_constraint"
    }
    source_check = checks["ck_exercise_import_alias_source"]
    for source in ("strong", "hevy", "fitbod", "table", "notes"):
        assert source in source_check
    assert checks["ck_exercise_import_alias_normalized_length"] == (
        "char_length(normalized_external_name) <= 3600"
    )


def test_downgrade_removes_only_new_alias_schema(monkeypatch) -> None:
    migration = _migration()
    operations = _Operations(_Bind(select_rows=[]))
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert operations.bind.calls[0][0].strip() == (
        "LOCK TABLE exercise_import_aliases IN SHARE ROW EXCLUSIVE MODE"
    )

    assert operations.calls == [
        (
            "drop_constraint",
            "uq_exercise_import_alias_user_source_name",
            "exercise_import_aliases",
            {"type_": "unique"},
        ),
        (
            "drop_constraint",
            "ck_exercise_import_alias_normalized_length",
            "exercise_import_aliases",
            {"type_": "check"},
        ),
        (
            "drop_constraint",
            "ck_exercise_import_alias_source",
            "exercise_import_aliases",
            {"type_": "check"},
        ),
        ("drop_column", "exercise_import_aliases", "normalized_external_name_sha256"),
        ("drop_column", "exercise_import_aliases", "normalized_external_name"),
    ]


def test_downgrade_reconciles_same_target_cross_source_rows(monkeypatch) -> None:
    migration = _migration()
    operations = _Operations(
        _Bind(
            select_rows=[
                {
                    "id": 11,
                    "app_user_id": 7,
                    "normalized_external_name": "strasse press",
                    "exercise_id": 101,
                },
                {
                    "id": 12,
                    "app_user_id": 7,
                    "normalized_external_name": "strasse press",
                    "exercise_id": 101,
                },
            ]
        )
    )
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    delete_calls = [
        (sql, parameters)
        for sql, parameters in operations.bind.calls
        if sql.lstrip().upper().startswith("DELETE")
    ]
    assert delete_calls == [
        (
            delete_calls[0][0],
            [{"row_id": 11}],
        )
    ]
    assert "WHERE id = :row_id" in delete_calls[0][0]
    assert operations.calls[-2:] == [
        ("drop_column", "exercise_import_aliases", "normalized_external_name_sha256"),
        ("drop_column", "exercise_import_aliases", "normalized_external_name"),
    ]


def test_downgrade_refuses_divergent_cross_source_targets(monkeypatch) -> None:
    migration = _migration()
    operations = _Operations(
        _Bind(
            select_rows=[
                {
                    "id": 11,
                    "app_user_id": 7,
                    "normalized_external_name": "strasse press",
                    "exercise_id": 101,
                },
                {
                    "id": 12,
                    "app_user_id": 7,
                    "normalized_external_name": "strasse press",
                    "exercise_id": 202,
                },
            ]
        )
    )
    monkeypatch.setattr(migration, "op", operations)

    with pytest.raises(RuntimeError, match="divergent exercise alias targets"):
        migration.downgrade()

    assert operations.calls == []
    assert not any(
        sql.lstrip().upper().startswith("DELETE")
        for sql, _ in operations.bind.calls
    )


def test_upgrade_refuses_to_merge_distinct_names_on_digest_collision(monkeypatch) -> None:
    migration = _migration()
    operations = _Operations()

    class _CollidingDigest:
        def hexdigest(self):
            return "0" * 64

    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(
        migration.hashlib,
        "sha256",
        lambda _value: _CollidingDigest(),
    )

    with pytest.raises(RuntimeError, match="digest collision"):
        migration.upgrade()

    update_batches = [
        parameters for _, parameters in operations.bind.calls if parameters
    ]
    assert update_batches == []
