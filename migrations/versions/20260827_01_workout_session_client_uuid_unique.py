"""Enforce per-user workout client UUID idempotency.

Revision ID: 20261018_p113_identity
Revises: 20261018_p113_display
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261018_p113_identity"
down_revision: Union[str, Sequence[str], None] = "20261018_p113_display"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # Serialize the duplicate audit and constraint creation with writers. We
    # refuse to discard ambiguous workout history automatically; an operator
    # can reconcile any legacy duplicates without losing user data.
    bind.execute(
        sa.text("LOCK TABLE workout_sessions IN SHARE ROW EXCLUSIVE MODE")
    )
    duplicate_identity_count = bind.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM (
                SELECT app_user_id, client_uuid
                FROM workout_sessions
                WHERE client_uuid IS NOT NULL
                GROUP BY app_user_id, client_uuid
                HAVING count(*) > 1
            ) AS duplicate_identities
            """
        )
    )
    if duplicate_identity_count:
        raise RuntimeError(
            "cannot enforce workout client UUID uniqueness: "
            f"{duplicate_identity_count} duplicate identities require reconciliation"
        )

    op.create_unique_constraint(
        "uq_workout_sessions_user_client_uuid",
        "workout_sessions",
        ["app_user_id", "client_uuid"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_workout_sessions_user_client_uuid",
        "workout_sessions",
        type_="unique",
    )
