"""Persist executable-session mode and explicit planned-row bindings.

Revision ID: 20261018_01_execution
Revises: 20261018_p113_identity
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '20261018_01_execution'
down_revision = '20261018_p113_identity'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('workout_sessions', sa.Column('execution_state', postgresql.JSONB(), nullable=True))
    op.add_column('workout_session_exercises', sa.Column('execution_state', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('workout_session_exercises', 'execution_state')
    op.drop_column('workout_sessions', 'execution_state')
