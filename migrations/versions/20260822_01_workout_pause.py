"""Persist paused workout lifecycle fields.

Revision ID: 20261018_p113_pause
Revises: 20261015_02
Create Date: 2026-08-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261018_p113_pause"
down_revision: Union[str, Sequence[str], None] = "20261015_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workout_sessions",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workout_sessions",
        sa.Column(
            "paused_duration_seconds",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.drop_constraint(
        "ck_workout_sessions_status", "workout_sessions", type_="check"
    )
    op.create_check_constraint(
        "ck_workout_sessions_status",
        "workout_sessions",
        "status IN ('active', 'paused', 'finished')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE workout_sessions "
        "SET status = 'active', paused_at = NULL "
        "WHERE status = 'paused'"
    )
    op.drop_constraint(
        "ck_workout_sessions_status", "workout_sessions", type_="check"
    )
    op.create_check_constraint(
        "ck_workout_sessions_status",
        "workout_sessions",
        "status IN ('active', 'finished')",
    )
    op.drop_column("workout_sessions", "paused_duration_seconds")
    op.drop_column("workout_sessions", "paused_at")
