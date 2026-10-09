"""Persist gym collars and scoped equipment selections.

Revision ID: 20261009_02
Revises: 20261009_01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261009_02"
down_revision = "20261009_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("gym_profiles", sa.Column("collars", postgresql.JSONB(), nullable=False,
                                           server_default=sa.text("'[]'::jsonb")))
    op.create_table(
        "exercise_equipment_preferences",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_user_id", sa.BigInteger(), nullable=False),
        sa.Column("gym_id", sa.UUID(), nullable=False),
        sa.Column("exercise_source", sa.String(10), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("context_key", sa.String(512), nullable=False),
        sa.Column("selected_bar", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("selected_collar_id", sa.UUID(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["gym_id"], ["gym_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_user_id", "gym_id", "exercise_source", "exercise_id", "context_key",
                            name="uq_equipment_preferences_owner_context"),
        sa.CheckConstraint("exercise_source IN ('global', 'user')", name="ck_equipment_preferences_source"),
        sa.CheckConstraint("exercise_id > 0", name="ck_equipment_preferences_exercise_positive"),
        sa.CheckConstraint("length(btrim(context_key)) > 0", name="ck_equipment_preferences_context_nonempty"),
        sa.CheckConstraint("selected_bar IS NULL OR jsonb_typeof(selected_bar) = 'object'",
                           name="ck_equipment_preferences_bar_object"),
        sa.CheckConstraint("revision > 0", name="ck_equipment_preferences_revision_positive"),
    )
    op.create_index("ix_exercise_equipment_preferences_app_user_id", "exercise_equipment_preferences", ["app_user_id"])


def downgrade() -> None:
    op.drop_index("ix_exercise_equipment_preferences_app_user_id", table_name="exercise_equipment_preferences")
    op.drop_table("exercise_equipment_preferences")
    op.drop_column("gym_profiles", "collars")
