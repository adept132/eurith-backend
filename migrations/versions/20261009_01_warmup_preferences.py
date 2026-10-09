"""Persist owner/source scoped warmup preferences.

Revision ID: 20261009_01
Revises: 20261007_01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261009_01"
down_revision = "20261007_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "exercise_warmup_preferences",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_user_id", sa.BigInteger(), nullable=False),
        sa.Column("exercise_source", sa.String(10), nullable=False),
        sa.Column("exercise_id", sa.Integer(), nullable=False),
        sa.Column("override", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_user_id", "exercise_source", "exercise_id",
                            name="uq_exercise_warmup_preferences_owner_exercise"),
        sa.CheckConstraint("exercise_source IN ('global', 'user')", name="ck_exercise_warmup_preferences_source"),
        sa.CheckConstraint("exercise_id > 0", name="ck_exercise_warmup_preferences_exercise_positive"),
        sa.CheckConstraint("jsonb_typeof(override) = 'object'", name="ck_exercise_warmup_preferences_override_object"),
        sa.CheckConstraint("revision > 0", name="ck_exercise_warmup_preferences_revision_positive"),
    )
    op.create_index("ix_exercise_warmup_preferences_app_user_id", "exercise_warmup_preferences", ["app_user_id"])


def downgrade() -> None:
    op.drop_index("ix_exercise_warmup_preferences_app_user_id", table_name="exercise_warmup_preferences")
    op.drop_table("exercise_warmup_preferences")
