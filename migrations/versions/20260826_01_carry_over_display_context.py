"""Persist immutable source exercise display context for carry-over previews.

Revision ID: 20260826_01
Revises: 20260822_09
Create Date: 2026-08-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260826_01"
down_revision: Union[str, Sequence[str], None] = "20260822_09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workout_carry_overs",
        sa.Column("source_exercise_name", sa.String(length=255), nullable=True),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE workout_carry_overs AS carry_over
            SET source_exercise_name = exercises.name
            FROM workout_session_exercises AS source_session_exercise
            JOIN exercises ON exercises.id = source_session_exercise.exercise_id
            WHERE source_session_exercise.id = carry_over.source_exercise_id
            """
        )
    )
    missing_snapshot_count = bind.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM workout_carry_overs
            WHERE source_exercise_name IS NULL
            """
        )
    )
    if missing_snapshot_count:
        raise RuntimeError(
            "cannot enforce carry-over source display context: "
            f"{missing_snapshot_count} rows could not be backfilled"
        )
    op.alter_column(
        "workout_carry_overs",
        "source_exercise_name",
        existing_type=sa.String(length=255),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("workout_carry_overs", "source_exercise_name")
