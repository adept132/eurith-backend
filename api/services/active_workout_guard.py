from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AppUser, WorkoutSession


class ActiveWorkoutExists(Exception):
    pass


async def ensure_no_other_active_workout(
    db: AsyncSession,
    user_id: int,
    *,
    exclude_workout_id: int | None = None,
) -> None:
    """Serialize every live-session creation on the same per-user row."""
    await db.execute(
        select(AppUser.id).where(AppUser.id == user_id).with_for_update()
    )
    query = select(WorkoutSession.id).where(
        WorkoutSession.app_user_id == user_id,
        WorkoutSession.status.in_(("active", "paused")),
    )
    if exclude_workout_id is not None:
        query = query.where(WorkoutSession.id != exclude_workout_id)
    if await db.scalar(query) is not None:
        raise ActiveWorkoutExists
