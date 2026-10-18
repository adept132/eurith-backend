from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, String, UniqueConstraint

from api.services import models


def _carry_over_model():
    assert hasattr(models, "WorkoutCarryOver"), (
        "WorkoutCarryOver must persist the immutable carry-over request"
    )
    return models.WorkoutCarryOver


def test_carry_over_has_source_uniqueness_and_restrictive_foreign_keys():
    carry_over = _carry_over_model()
    table = carry_over.__table__

    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("app_user_id", "source_session_id", "source_exercise_id") in unique_columns
    assert ("app_user_id", "idempotency_key") in unique_columns

    source_session_fk = next(iter(table.c.source_session_id.foreign_keys))
    source_exercise_fk = next(iter(table.c.source_exercise_id.foreign_keys))
    assert source_session_fk.target_fullname == "workout_sessions.id"
    assert source_session_fk.ondelete == "RESTRICT"
    assert source_exercise_fk.target_fullname == "workout_session_exercises.id"
    assert source_exercise_fk.ondelete == "RESTRICT"


def test_carry_over_persists_a_required_source_exercise_name_snapshot():
    column = _carry_over_model().__table__.c.source_exercise_name

    assert isinstance(column.type, String)
    assert column.type.length == 255
    assert column.nullable is False


def test_display_context_migration_backfills_legacy_placement_snapshots():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260826_01_carry_over_display_context.py"
    )
    migration = migration_path.read_text(encoding="utf-8")

    assert models.WorkoutCarryOver.__tablename__ == "workout_carry_overs"
    assert models.Exercise.__tablename__ == "exercises"
    assert models.UserCalendarDay.__tablename__ == "user_calendar"
    assert models.WorkoutPlan.__tablename__ == "workout_plans"
    assert "jsonb_array_elements" in migration
    assert "calendar_day_id" in migration
    assert "calendar_day.plan_id" in migration
    assert "workout_plans" in migration
    assert "target_exercise_name" in migration
    assert "target_day_name" in migration
    assert "cannot enforce carry-over placement display context" in migration


def test_carry_over_database_checks_counts_and_statuses():
    carry_over = _carry_over_model()
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in carry_over.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "requested_sets >= 0" in checks["ck_workout_carry_over_requested_sets"]
    assert "placed_sets >= 0" in checks["ck_workout_carry_over_placed_sets"]
    assert "discarded_sets >= 0" in checks["ck_workout_carry_over_discarded_sets"]
    assert (
        "requested_sets = placed_sets + discarded_sets"
        in checks["ck_workout_carry_over_counts_consistent"]
    )
    for status in ("draft", "applied", "dismissed", "expired"):
        assert status in checks["ck_workout_carry_over_status"]


def test_carry_over_schema_rejects_inconsistent_counts_and_discard_reasons():
    assert importlib.util.find_spec("api.schemas.carry_over") is not None, (
        "carry-over schemas must exist"
    )
    schema = importlib.import_module("api.schemas.carry_over")

    valid = schema.CarryOverPreviewItem(
        carry_over_id=7,
        source_exercise_id=11,
        source_exercise_name="  Bench press  ",
        requested_sets=4,
        placed_sets=2,
        discarded_sets=2,
        placements=[
            {
                "calendar_day_id": 31,
                "target_date": "2026-08-24",
                "exercise_id": 5,
                "target_exercise_name": "  Incline bench press  ",
                "target_day_name": "  Sunday upper body  ",
                "delta_sets": 2,
                "match_kind": "exact",
            }
        ],
        discarded=[{"sets": 2, "reason": "session_cap"}],
    )
    assert valid.requested_sets == 4
    assert valid.source_exercise_name == "Bench press"
    assert valid.placements[0].target_exercise_name == "Incline bench press"
    assert valid.placements[0].target_day_name == "Sunday upper body"

    with pytest.raises(ValidationError):
        schema.CarryOverResponse(
            status="draft",
            source_session_id=7,
            microcycle_end="2026-08-24",
            preview_token="A" * 64,
            items=[],
        )

    with pytest.raises(ValidationError):
        schema.CarryOverPreviewItem(
            carry_over_id=7,
            source_exercise_id=11,
            requested_sets=4,
            placed_sets=0,
            discarded_sets=4,
            discarded=[{"sets": 4, "reason": "session_cap"}],
        )

    with pytest.raises(ValidationError):
        schema.CarryOverPreviewItem(
            carry_over_id=7,
            source_exercise_id=11,
            source_exercise_name="",
            requested_sets=4,
            placed_sets=2,
            discarded_sets=2,
            placements=[
                {
                    "calendar_day_id": 31,
                    "target_date": "2026-08-24",
                    "exercise_id": 5,
                    "target_exercise_name": "",
                    "target_day_name": "Sunday upper body",
                    "delta_sets": 2,
                    "match_kind": "exact",
                }
            ],
            discarded=[{"sets": 2, "reason": "session_cap"}],
        )

    with pytest.raises(ValidationError):
        schema.CarryOverPreviewItem(
            carry_over_id=7,
            source_exercise_id=11,
            source_exercise_name="Bench press",
            requested_sets=4,
            placed_sets=2,
            discarded_sets=2,
            placements=[
                {
                    "calendar_day_id": 31,
                    "target_date": "2026-08-24",
                    "exercise_id": 5,
                    "target_exercise_name": "Incline bench press",
                    "target_day_name": "x" * 256,
                    "delta_sets": 2,
                    "match_kind": "exact",
                }
            ],
            discarded=[{"sets": 2, "reason": "session_cap"}],
        )

    with pytest.raises(ValidationError):
        schema.CarryOverPreviewItem(
            carry_over_id=7,
            source_exercise_id=11,
            source_exercise_name="Bench press",
            requested_sets=4,
            placed_sets=3,
            discarded_sets=0,
            placements=[],
            discarded=[],
        )

    with pytest.raises(ValidationError):
        schema.CarryOverPreviewItem(
            carry_over_id=7,
            source_exercise_id=11,
            source_exercise_name="Bench press",
            requested_sets=4,
            placed_sets=2,
            discarded_sets=2,
            placements=[],
            discarded=[{"sets": 1, "reason": "session_cap"}],
        )
