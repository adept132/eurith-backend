"""Add immutable workout-plan version provenance.

Revision ID: 20260926_05
Revises: 20260926_04
Create Date: 2026-08-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260926_05"
down_revision: Union[str, Sequence[str], None] = "20260926_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workout_plans",
        sa.Column("supersedes_plan_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "workout_plans",
        sa.Column(
            "is_archived",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "workout_plans",
        sa.Column(
            "revision",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_workout_plans_supersedes_plan_id",
        "workout_plans",
        "workout_plans",
        ["supersedes_plan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "plan_replacement_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("app_user_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("new_plan_id", sa.Integer(), nullable=False),
        sa.Column("affected_dates", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["app_user_id"], ["app_users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["new_plan_id"], ["workout_plans.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "app_user_id",
            "idempotency_key",
            name="uq_plan_replacement_operation_user_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("plan_replacement_operations")
    op.drop_constraint(
        "fk_workout_plans_supersedes_plan_id",
        "workout_plans",
        type_="foreignkey",
    )
    op.drop_column("workout_plans", "revision")
    op.drop_column("workout_plans", "is_archived")
    op.drop_column("workout_plans", "supersedes_plan_id")
