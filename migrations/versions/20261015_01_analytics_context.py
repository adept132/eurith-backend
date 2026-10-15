"""Persist optional historical load and calendar context."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = '20261015_01'
down_revision = '20261009_03'
branch_labels = None
depends_on = None

JSON_COLUMNS = {
    'workout_sessions': ['analytics_snapshot'],
    'workout_session_exercises': ['analytics_load_defaults'],
    'workout_session_sets': ['analytics_load_snapshot', 'anomaly_review'],
    'user_calendar': ['obligation_snapshot'],
}


def upgrade():
    for table, columns in JSON_COLUMNS.items():
        for column in columns:
            op.add_column(table, sa.Column(column, pg.JSONB(none_as_null=True), nullable=True))
    op.add_column('user_calendar', sa.Column('obligation_uuid', pg.UUID(as_uuid=True), nullable=True))
    op.add_column('user_calendar', sa.Column('obligation_origin_date', sa.Date(), nullable=True))
    op.create_index('ix_user_calendar_obligation_uuid', 'user_calendar', ['obligation_uuid'])


def downgrade():
    op.drop_index('ix_user_calendar_obligation_uuid', table_name='user_calendar')
    op.drop_column('user_calendar', 'obligation_origin_date')
    op.drop_column('user_calendar', 'obligation_uuid')
    for table, columns in reversed(list(JSON_COLUMNS.items())):
        for column in reversed(columns):
            op.drop_column(table, column)
