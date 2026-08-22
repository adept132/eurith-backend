"""Persist distributed workout carry-over previews and decisions.

Revision ID: 20260822_05
Revises: 20260822_04
Create Date: 2026-08-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260822_05"
down_revision: Union[str, Sequence[str], None] = "20260822_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workout_carry_overs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_session_id", sa.Integer(), nullable=False),
        sa.Column("source_exercise_id", sa.Integer(), nullable=False),
        sa.Column("requested_sets", sa.Integer(), nullable=False),
        sa.Column("placed_sets", sa.Integer(), nullable=False),
        sa.Column("discarded_sets", sa.Integer(), nullable=False),
        sa.Column(
            "placements",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "discarded",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("preview_token", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="draft",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "requested_sets >= 0", name="ck_workout_carry_over_requested_sets"
        ),
        sa.CheckConstraint(
            "placed_sets >= 0", name="ck_workout_carry_over_placed_sets"
        ),
        sa.CheckConstraint(
            "discarded_sets >= 0", name="ck_workout_carry_over_discarded_sets"
        ),
        sa.CheckConstraint(
            "requested_sets = placed_sets + discarded_sets",
            name="ck_workout_carry_over_counts_consistent",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'applied', 'dismissed', 'expired')",
            name="ck_workout_carry_over_status",
        ),
        sa.ForeignKeyConstraint(
            ["app_user_id"], ["app_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_session_id"], ["workout_sessions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_exercise_id"],
            ["workout_session_exercises.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "app_user_id",
            "source_session_id",
            "source_exercise_id",
            name="uq_workout_carry_over_source",
        ),
        sa.UniqueConstraint(
            "app_user_id",
            "idempotency_key",
            name="uq_workout_carry_over_user_idempotency",
        ),
    )
    op.create_index(
        "ix_workout_carry_overs_app_user_id",
        "workout_carry_overs",
        ["app_user_id"],
    )
    op.create_index(
        "ix_workout_carry_overs_source_session_id",
        "workout_carry_overs",
        ["source_session_id"],
    )
    op.create_index(
        "ix_workout_carry_overs_source_exercise_id",
        "workout_carry_overs",
        ["source_exercise_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workout_carry_overs_source_exercise_id",
        table_name="workout_carry_overs",
    )
    op.drop_index(
        "ix_workout_carry_overs_source_session_id",
        table_name="workout_carry_overs",
    )
    op.drop_index(
        "ix_workout_carry_overs_app_user_id",
        table_name="workout_carry_overs",
    )
    op.drop_table("workout_carry_overs")
