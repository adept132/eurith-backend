"""Add private body progress photos and local body measurement dates."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_01"
down_revision: Union[str, Sequence[str], None] = "20260926_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_anthropometry", sa.Column("measured_on", sa.Date(), nullable=True))
    op.add_column("user_anthropometry", sa.Column("submitted_fields", postgresql.JSONB(), nullable=True))
    op.add_column("body_measurements", sa.Column("measured_on", sa.Date(), nullable=True))
    op.create_table(
        "body_progress_photos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("app_user_id", sa.BigInteger(), sa.ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("taken_on", sa.Date(), nullable=False),
        sa.Column("angle", sa.String(20), nullable=False),
        sa.Column("storage_key", sa.String(160), nullable=False),
        sa.Column("thumbnail_key", sa.String(160), nullable=False),
        sa.Column("mime_type", sa.String(32), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("client_uuid", sa.String(64), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("app_user_id", "client_uuid", name="uq_body_photos_user_client_uuid"),
        sa.CheckConstraint("angle IN ('front', 'side', 'back', 'unspecified')", name="ck_body_photos_angle"),
        sa.CheckConstraint("state IN ('active', 'deleting')", name="ck_body_photos_state"),
    )
    op.create_index("ix_body_photos_user_date", "body_progress_photos", ["app_user_id", "taken_on", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_body_photos_user_date", table_name="body_progress_photos")
    op.drop_table("body_progress_photos")
    op.drop_column("body_measurements", "measured_on")
    op.drop_column("user_anthropometry", "submitted_fields")
    op.drop_column("user_anthropometry", "measured_on")
