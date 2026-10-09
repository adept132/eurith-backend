"""Preserve warmup calculation facts and scoped deleted child identities.

Revision ID: 20261009_03
Revises: 20261009_02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261009_03"
down_revision = "20261009_02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("workout_session_exercises", sa.Column("warmup_group", postgresql.JSONB(none_as_null=True), nullable=True))
    op.add_column("workout_session_sets", sa.Column("warmup_snapshot", postgresql.JSONB(none_as_null=True), nullable=True))
    op.add_column("sync_tombstones", sa.Column("workout_session_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_sync_tombstones_workout", "sync_tombstones", "workout_sessions", ["workout_session_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_sync_tombstones_workout_session_id", "sync_tombstones", ["workout_session_id"])
    op.alter_column("sync_tombstones", "client_uuid", existing_type=sa.String(64), nullable=True)


def downgrade():
    # Only this feature's child deletion metadata disappears on downgrade.
    op.execute("DELETE FROM sync_tombstones WHERE entity_type IN ('workout_set', 'workout_exercise')")
    op.alter_column("sync_tombstones", "client_uuid", existing_type=sa.String(64), nullable=False)
    op.drop_index("ix_sync_tombstones_workout_session_id", table_name="sync_tombstones")
    op.drop_constraint("fk_sync_tombstones_workout", "sync_tombstones", type_="foreignkey")
    op.drop_column("sync_tombstones", "workout_session_id")
    op.drop_column("workout_session_sets", "warmup_snapshot")
    op.drop_column("workout_session_exercises", "warmup_group")
