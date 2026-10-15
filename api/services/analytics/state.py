"""Transaction-owned revision state; callers decide when to commit."""
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AnalyticsState
from .policy import RULES_VERSION
from .config import analytics_enabled


async def readiness(db: AsyncSession, user_id: int, *, expected_revision: int | None = None) -> dict:
    """Read-only gate shared by routes and report/background materializers.

    Check once before loading facts, then again with their source_revision after
    assembly. Only expose calculations if the final state is ready; this also
    protects batched READ COMMITTED loads from intervening source changes.
    For compositions, require every nested DTO revision to equal the supplied
    expected_revision before calling this gate (including both compared blocks).
    """
    row = (await db.execute(select(AnalyticsState.source_revision,
        AnalyticsState.active_revision, AnalyticsState.active_generation,
        AnalyticsState.status).where(AnalyticsState.app_user_id == user_id,
        AnalyticsState.rules_version == RULES_VERSION))).one_or_none()
    revision = row.source_revision if row else 0
    ready = (row is not None and row.status == 'ready' and row.active_generation is not None
             and row.active_revision == revision
             and (expected_revision is None or expected_revision == revision))
    return dict(state='disabled' if not analytics_enabled() else 'ready' if ready else 'rebuilding',
                data_revision=revision, active_revision=row.active_revision if row else None,
                rules_version=RULES_VERSION)


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
