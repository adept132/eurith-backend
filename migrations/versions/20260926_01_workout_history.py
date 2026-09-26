"""Add editable workout history and durable recalculation operations.

Revision ID: 20260926_01
Revises: 20260908_01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260926_01"
down_revision: Union[str, Sequence[str], None] = "20260908_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workout_sessions",
        sa.Column("entry_mode", sa.String(length=32), server_default="live", nullable=False),
    )
    op.add_column(
        "workout_sessions",
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("workout_sessions", sa.Column("edited_at", sa.DateTime(timezone=True)))
    op.add_column("workout_sessions", sa.Column("history_request_fingerprint", sa.String(length=64)))
    op.create_check_constraint(
        "ck_workout_sessions_entry_mode",
        "workout_sessions",
        "entry_mode IN ('live', 'manual', 'screenshot_import')",
    )

    # Старые строки не имели идентификаторов дочерних сущностей. Они нужны,
    # чтобы редактор сохранял снимок без потери предписаний и связей подходов.
    op.execute(
        "UPDATE workout_sessions SET client_uuid = 'server-workout-' || id "
        "WHERE client_uuid IS NULL"
    )
    op.execute(
        "UPDATE workout_session_exercises SET client_uuid = 'server-exercise-' || id "
        "WHERE client_uuid IS NULL"
    )
    op.execute(
        "UPDATE workout_session_sets SET client_uuid = 'server-set-' || id "
        "WHERE client_uuid IS NULL"
    )

    op.create_table(
        "workout_recalculation_operations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workout_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("exercise_ids", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_workout_recalculation_status",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_workout_recalculation_revision"),
        sa.CheckConstraint("attempts >= 0", name="ck_workout_recalculation_attempts"),
        sa.ForeignKeyConstraint(["workout_id"], ["workout_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workout_id", "revision", name="uq_workout_recalculation_revision"),
    )
    op.create_index(
        "ix_workout_recalculation_operations_workout_id",
        "workout_recalculation_operations",
        ["workout_id"],
    )
    op.create_index(
        "ix_workout_recalculation_ready",
        "workout_recalculation_operations",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workout_recalculation_ready", table_name="workout_recalculation_operations")
    op.drop_index("ix_workout_recalculation_operations_workout_id", table_name="workout_recalculation_operations")
    op.drop_table("workout_recalculation_operations")
    op.drop_constraint("ck_workout_sessions_entry_mode", "workout_sessions", type_="check")
    op.drop_column("workout_sessions", "history_request_fingerprint")
    op.drop_column("workout_sessions", "edited_at")
    op.drop_column("workout_sessions", "revision")
    op.drop_column("workout_sessions", "entry_mode")
