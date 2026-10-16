"""Coherent publication shared by HTTP and snapshot materializers."""
from datetime import datetime, timezone
from pydantic import BaseModel
from api.schemas.analytics import AnalyticsEnvelope, AnalyticsUnavailable
from . import state


def _models(value):
    if isinstance(value, BaseModel):
        yield value
        children = (getattr(value, name) for name in type(value).model_fields)
    elif isinstance(value, dict):
        children = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    else:
        return
    for child in children:
        # Primitive leaves cannot contain a revision or envelope. Avoid a
        # recursive generator call for every scalar in large observation DTOs.
        if isinstance(child, (BaseModel, dict, list, tuple)):
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
    models = tuple(_models(resource))
    coherent = all(getattr(model, 'data_revision', expected) == expected for model in models)
    final = await state.readiness(db, user_id, expected_revision=expected)
    if initial['state'] != 'ready' or final['state'] != 'ready' or not coherent:
        unavailable_state = 'disabled' if final['state'] == 'disabled' else 'rebuilding'
        return AnalyticsUnavailable(state=unavailable_state, data_revision=final['data_revision'],
                                    computed_at=datetime.now(timezone.utc))
    for model in models:
        if isinstance(model, AnalyticsEnvelope):
            model.state = 'ready'
    return resource
