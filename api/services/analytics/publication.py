"""Coherent publication shared by HTTP and snapshot materializers."""
from datetime import datetime, timezone
from pydantic import BaseModel
from api.schemas.analytics import AnalyticsEnvelope, AnalyticsUnavailable
from . import state


def _models(value):
    seen, kinds = set(), {}

    def kind(item):
        cls = type(item)
        result = kinds.get(cls)
        if result is None:
            # Exact runtime class dispatch, including subclasses. Cache only
            # within this traversal; never skip fields based on their schema.
            result = (1 if issubclass(cls, BaseModel) else 2 if issubclass(cls, dict)
                      else 3 if issubclass(cls, (list, tuple)) else 0)
            kinds[cls] = result
        return result

    def walk(item, category):
        if not category or id(item) in seen:
            return
        seen.add(id(item))
        if category == 1:
            yield item
            children = (getattr(item, name) for name in type(item).model_fields)
        elif category == 2:
            children = item.values()
        else:
            children = item
        for child in children:
            child_kind = kind(child)
            if child_kind:
                yield from walk(child, child_kind)

    yield from walk(value, kind(value))


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
