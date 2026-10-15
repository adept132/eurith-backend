"""Transaction-owned revision state; callers decide when to commit."""
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AnalyticsState
from .policy import RULES_VERSION


async def mark_dirty(db: AsyncSession, user_id: int) -> int:
    statement = insert(AnalyticsState).values(
        app_user_id=user_id, rules_version=RULES_VERSION, source_revision=1, status='pending',
    ).on_conflict_do_update(
        index_elements=['app_user_id', 'rules_version'],
        set_={'source_revision': AnalyticsState.source_revision + 1,
              'status': 'pending', 'updated_at': func.now()},
    ).returning(AnalyticsState.source_revision)
    return (await db.execute(statement)).scalar_one()


async def read_state(db: AsyncSession, user_id: int) -> AnalyticsState:
    await db.execute(insert(AnalyticsState).values(
        app_user_id=user_id, rules_version=RULES_VERSION,
    ).on_conflict_do_nothing(index_elements=['app_user_id', 'rules_version']))
    return (await db.execute(select(AnalyticsState).where(
        AnalyticsState.app_user_id == user_id, AnalyticsState.rules_version == RULES_VERSION,
    ).execution_options(populate_existing=True))).scalar_one()
