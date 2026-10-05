"""Add metadata-only workout import ML request records.

Revision ID: 20260926_04
Revises: 20260926_03
Create Date: 2026-08-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_04"
down_revision: Union[str, Sequence[str], None] = "20260926_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workout_import_ml_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("app_user_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("image_count BETWEEN 1 AND 3", name="ck_workout_import_ml_image_count"),
        sa.CheckConstraint("byte_count BETWEEN 1 AND 8388608", name="ck_workout_import_ml_byte_count"),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_workout_import_ml_latency"),
        sa.CheckConstraint(
                "status IN ('reserved', 'completed', 'rate_limited', 'timeout', 'unavailable', 'invalid_response', 'invalid_image', 'payload_too_large')",
            name="ck_workout_import_ml_status",
        ),
        sa.ForeignKeyConstraint(["app_user_id"], ["app_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_workout_import_ml_request_id"),
        sa.UniqueConstraint(
            "app_user_id", "idempotency_key", name="uq_workout_import_ml_user_idempotency"
        ),
    )
    op.create_index(
        "ix_workout_import_ml_requests_app_user_id",
        "workout_import_ml_requests",
        ["app_user_id"],
    )
    op.create_index(
        "ix_workout_import_ml_requests_created_at",
        "workout_import_ml_requests",
        ["created_at"],
    )
    op.create_index(
        "ix_workout_import_ml_user_created",
        "workout_import_ml_requests",
        ["app_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_workout_import_ml_user_created", table_name="workout_import_ml_requests")
    op.drop_index("ix_workout_import_ml_requests_created_at", table_name="workout_import_ml_requests")
    op.drop_index("ix_workout_import_ml_requests_app_user_id", table_name="workout_import_ml_requests")
    op.drop_table("workout_import_ml_requests")
