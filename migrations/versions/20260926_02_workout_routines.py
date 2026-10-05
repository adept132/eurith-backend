"""Add standalone workout routines and routine session provenance.

Revision ID: 20260926_02
Revises: 20260926_01
Create Date: 2026-09-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260926_02"
down_revision: Union[str, Sequence[str], None] = "20260926_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workout_routines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_user_id", sa.BigInteger(), nullable=False),
        sa.Column("client_uuid", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("sort_order >= 0", name="ck_workout_routines_sort_order"),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "app_user_id", "client_uuid", name="uq_workout_routines_user_client_uuid"
        ),
    )
    op.create_index(
        "ix_workout_routines_app_user_id", "workout_routines", ["app_user_id"]
    )

    op.create_table(
        "workout_routine_exercises",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("routine_id", sa.Integer(), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("superset_group", sa.String(length=64), nullable=True),
        sa.Column("target_sets", sa.Integer(), nullable=False),
        sa.Column("set_kinds", postgresql.JSONB(), nullable=False),
        sa.Column("rep_min", sa.Integer(), nullable=True),
        sa.Column("rep_max", sa.Integer(), nullable=True),
        sa.Column("target_rir", sa.Integer(), nullable=True),
        sa.Column("rest_seconds", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "order_index >= 0", name="ck_workout_routine_exercise_order"
        ),
        sa.CheckConstraint(
            "target_sets > 0", name="ck_workout_routine_exercise_target_sets"
        ),
        sa.CheckConstraint(
            "rep_min IS NULL OR rep_min > 0",
            name="ck_workout_routine_exercise_rep_min",
        ),
        sa.CheckConstraint(
            "rep_max IS NULL OR rep_max >= rep_min",
            name="ck_workout_routine_exercise_rep_max",
        ),
        sa.CheckConstraint(
            "target_rir IS NULL OR target_rir BETWEEN 0 AND 10",
            name="ck_workout_routine_exercise_target_rir",
        ),
        sa.CheckConstraint(
            "rest_seconds IS NULL OR rest_seconds >= 0",
            name="ck_workout_routine_exercise_rest_seconds",
        ),
        sa.ForeignKeyConstraint(["exercise_id"], ["exercises.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["routine_id"], ["workout_routines.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "routine_id", "order_index", name="uq_workout_routine_exercise_order"
        ),
    )
    op.create_index(
        "ix_workout_routine_exercises_routine_id",
        "workout_routine_exercises",
        ["routine_id"],
    )
    op.create_index(
        "ix_workout_routine_exercises_exercise_id",
        "workout_routine_exercises",
        ["exercise_id"],
    )

    op.add_column(
        "workout_sessions", sa.Column("routine_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_workout_sessions_routine_id",
        "workout_sessions",
        "workout_routines",
        ["routine_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_workout_sessions_routine_id", "workout_sessions", ["routine_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_workout_sessions_routine_id", table_name="workout_sessions")
    op.drop_constraint(
        "fk_workout_sessions_routine_id", "workout_sessions", type_="foreignkey"
    )
    op.drop_column("workout_sessions", "routine_id")
    op.drop_index(
        "ix_workout_routine_exercises_exercise_id",
        table_name="workout_routine_exercises",
    )
    op.drop_index(
        "ix_workout_routine_exercises_routine_id",
        table_name="workout_routine_exercises",
    )
    op.drop_table("workout_routine_exercises")
    op.drop_index("ix_workout_routines_app_user_id", table_name="workout_routines")
    op.drop_table("workout_routines")
