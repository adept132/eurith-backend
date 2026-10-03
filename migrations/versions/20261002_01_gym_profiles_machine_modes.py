"""Add saved gym profiles, machine modes, and workout load snapshots.

Revision ID: 20261002_01
Revises: 20260908_01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20261002_01"
down_revision: Union[str, Sequence[str], None] = "20260908_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gym_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("app_user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("equipment", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("bars", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("discs", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision > 0", name="ck_gym_profiles_revision_positive"),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_gym_profiles_owner_name",
        "gym_profiles",
        ["app_user_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "gym_exercise_setups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gym_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("exercise_source", sa.String(length=10), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("load_mode", sa.String(length=20), nullable=False),
        sa.Column("is_available", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_preferred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("step_value", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("step_unit", sa.String(length=12), nullable=False),
        sa.Column("loading_sides", sa.Integer(), server_default="1", nullable=False),
        sa.Column("base_weight", sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column("weight_basis", sa.String(length=20), nullable=False),
        sa.Column("plate_inventory", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("exercise_source IN ('global', 'user')", name="ck_gym_setups_exercise_source"),
        sa.CheckConstraint("load_mode IN ('stack', 'plate_loaded')", name="ck_gym_setups_load_mode"),
        sa.CheckConstraint("step_value > 0", name="ck_gym_setups_step_positive"),
        sa.CheckConstraint("loading_sides IN (1, 2)", name="ck_gym_setups_loading_sides"),
        sa.CheckConstraint("revision > 0", name="ck_gym_setups_revision_positive"),
        sa.CheckConstraint("base_weight IS NULL OR base_weight >= 0", name="ck_gym_setups_base_weight_nonnegative"),
        sa.CheckConstraint(
            "plate_inventory IS NULL OR (jsonb_typeof(plate_inventory) = 'array' "
            "AND NOT jsonb_path_exists(plate_inventory, '$[*].count ? (@ < 0)'))",
            name="ck_gym_setups_plate_counts_nonnegative",
        ),
        sa.ForeignKeyConstraint(["gym_id"], ["gym_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_gym_setups_active_mode",
        "gym_exercise_setups",
        ["gym_id", "exercise_source", "exercise_id", "load_mode"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "exercise_load_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("app_user_id", sa.BigInteger(), nullable=False),
        sa.Column("exercise_source", sa.String(length=10), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("enabled_modes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("preferred_mode", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("exercise_source IN ('global', 'user')", name="ck_exercise_load_preferences_source"),
        sa.CheckConstraint(
            "jsonb_typeof(enabled_modes) = 'array' AND jsonb_array_length(enabled_modes) > 0 "
            "AND enabled_modes <@ '[\"stack\", \"plate_loaded\"]'::jsonb",
            name="ck_exercise_load_preferences_enabled_modes",
        ),
        sa.CheckConstraint(
            "preferred_mode IN ('stack', 'plate_loaded') "
            "AND enabled_modes @> jsonb_build_array(preferred_mode)",
            name="ck_exercise_load_preferences_preferred_mode",
        ),
        sa.CheckConstraint("revision > 0", name="ck_exercise_load_preferences_revision_positive"),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "app_user_id", "exercise_source", "exercise_id",
            name="uq_exercise_load_preferences_owner_exercise",
        ),
    )
    op.create_index("ix_exercise_load_preferences_app_user_id", "exercise_load_preferences", ["app_user_id"])

    for table, columns in (
        ("workout_plans", (
            ("gym_profile_id", postgresql.UUID(as_uuid=True)),
            ("gym_snapshot", postgresql.JSONB(astext_type=sa.Text())),
        )),
        ("workout_plan_exercises", (
            ("load_mode", sa.String(length=20)),
            ("setup_id", postgresql.UUID(as_uuid=True)),
            ("load_snapshot", postgresql.JSONB(astext_type=sa.Text())),
        )),
        ("workout_sessions", (
            ("gym_profile_id", postgresql.UUID(as_uuid=True)),
            ("gym_snapshot", postgresql.JSONB(astext_type=sa.Text())),
        )),
        ("workout_session_exercises", (
            ("active_load_mode", sa.String(length=20)),
            ("active_setup_id", postgresql.UUID(as_uuid=True)),
        )),
        ("workout_session_sets", (
            ("load_mode", sa.String(length=20)),
            ("gym_profile_id", postgresql.UUID(as_uuid=True)),
            ("setup_id", postgresql.UUID(as_uuid=True)),
            ("load_snapshot", postgresql.JSONB(astext_type=sa.Text())),
            ("shown_target_snapshot", postgresql.JSONB(astext_type=sa.Text())),
        )),
    ):
        for name, column_type in columns:
            op.add_column(table, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    for table, columns in (
        ("workout_session_sets", ("shown_target_snapshot", "load_snapshot", "setup_id", "gym_profile_id", "load_mode")),
        ("workout_session_exercises", ("active_setup_id", "active_load_mode")),
        ("workout_sessions", ("gym_snapshot", "gym_profile_id")),
        ("workout_plan_exercises", ("load_snapshot", "setup_id", "load_mode")),
        ("workout_plans", ("gym_snapshot", "gym_profile_id")),
    ):
        for name in columns:
            op.drop_column(table, name)

    op.drop_index("ix_exercise_load_preferences_app_user_id", table_name="exercise_load_preferences")
    op.drop_table("exercise_load_preferences")
    op.drop_index("uq_gym_setups_active_mode", table_name="gym_exercise_setups")
    op.drop_table("gym_exercise_setups")
    op.drop_table("gym_profiles")
