"""Coherent publication shared by HTTP and snapshot materializers."""
from datetime import datetime, timezone
from pydantic import BaseModel
from api.schemas.analytics import AnalyticsEnvelope, AnalyticsUnavailable
from . import state


def _models(value):
    if isinstance(value, BaseModel):
        yield value
        for name in type(value).model_fields:
            yield from _models(getattr(value, name))
    elif isinstance(value, dict):
        for child in value.values():
            yield from _models(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _models(child)


async def coherent_read(db, user_id, assemble):
    """Fence BEFORE any facts, verify every nested revision, then fence again.

    Assemble is an async no-argument callable. It must be read-only and retain
    owner validation. Unavailable results have no calculated numeric payload.
    No commits or rebuilds are performed here; snapshot callers own persistence.
    """
    initial = await state.readiness(db, user_id)
    resource = await assemble()
    expected = initial['data_revision']
    coherent = all(getattr(model, 'data_revision', expected) == expected for model in _models(resource))
    final = await state.readiness(db, user_id, expected_revision=expected)
    if initial['state'] != 'ready' or final['state'] != 'ready' or not coherent:
        unavailable_state = 'disabled' if final['state'] == 'disabled' else 'rebuilding'
        return AnalyticsUnavailable(state=unavailable_state, data_revision=final['data_revision'],
                                    computed_at=datetime.now(timezone.utc))
    for model in _models(resource):
        if isinstance(model, AnalyticsEnvelope):
            model.state = 'ready'
    return resource
