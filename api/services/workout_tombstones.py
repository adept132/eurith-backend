"""Durable, workout-scoped child deletion identities; never workout tombstones."""
from datetime import datetime, timezone

from sqlalchemy import or_, select
from api.services.models import SyncTombstone


async def child_was_deleted(db, workout, entity_type, client_uuid, server_id=None):
    identities = []
    if client_uuid is not None:
        identities.append(SyncTombstone.client_uuid == client_uuid)
    if server_id is not None:
        identities.append(SyncTombstone.server_id == server_id)
    if not identities:
        return False
    return bool(await db.scalar(select(SyncTombstone.id).where(
        SyncTombstone.app_user_id == workout.app_user_id,
        SyncTombstone.workout_session_id == workout.id,
        SyncTombstone.entity_type == entity_type,
        or_(*identities),
    ).limit(1)))


async def record_child_deletion(db, workout, entity_type, client_uuid, server_id=None):
    if await child_was_deleted(db, workout, entity_type, client_uuid, server_id):
        return
    db.add(SyncTombstone(app_user_id=workout.app_user_id, workout_session_id=workout.id,
        entity_type=entity_type, client_uuid=client_uuid, server_id=server_id))
    workout.updated_at = datetime.now(timezone.utc)
    workout.sync_version = (workout.sync_version or 0) + 1
