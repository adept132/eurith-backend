"""Pack already-fenced typed resources without truncating facts or changing math."""
import json
from pydantic_core import to_jsonable_python
from api.schemas.analytics import AnalyticsUnavailable
from api.schemas.analytics_compact import (AnalyticsCompactV1, CompactPointDetails,
    CompactPoint, CompactExercise, CompactContribution, CompactMuscle,
    CompactMuscles, CompactBlock, CompactComparison)


def _fields(model, **changes):
    return {name: changes[name] if name in changes else getattr(model, name)
            for name in type(model).model_fields}


def pack_resource(resource, kind, owner_uid):
    if isinstance(resource, AnalyticsUnavailable):
        return resource
    contexts, metrics, points = [], [], []
    context_keys, context_ids, metric_keys, point_ids = {}, {}, {}, {}

    def context(value):
        if value is None:
            return None
        identity = id(value)
        if identity in context_ids:
            return context_ids[identity]
        key = json.dumps(value, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, default=to_jsonable_python)
        if key not in context_keys:
            context_keys[key] = len(contexts)
            contexts.append(value)
        context_ids[identity] = context_keys[key]
        return context_keys[key]

    def metric(value):
        coverage = (value.coverage.used, value.coverage.total) if value.coverage is not None else None
        key = (float(value.value).hex() if value.value is not None else None, value.reason, coverage)
        if key not in metric_keys:
            metric_keys[key] = len(metrics)
            metrics.append((value.value, value.reason, coverage))
        return metric_keys[key]

    def point(value):
        identity = id(value)
        if identity in point_ids:
            return point_ids[identity]
        rows = [(r.set_id, r.exercise_instance_id, r.set_number, r.setup_id,
            r.gym_id, r.gym_label, r.at, r.observation_date, r.set_type,
            r.actual_reps, r.e1rm_reps, r.e1rm_weight_kg, r.tonnage_kg, r.rir,
            metric(r.intensity), context(r.load_context), context(r.session_context), r.reasons)
            for r in value.details.observations]
        details = CompactPointDetails.model_construct(**_fields(value.details, observations=rows))
        packed = CompactPoint.model_construct(**_fields(value, details=details))
        index = len(points)
        point_ids[identity] = index
        points.append(packed)
        return index

    def exercise(value):
        return CompactExercise.model_construct(**_fields(value, points=[point(p) for p in value.points]))

    def muscle(value):
        contributions = tuple(CompactContribution.model_construct(**_fields(c,
            observations=tuple(point(p) for p in c.observations))) for c in value.contributions)
        return CompactMuscle.model_construct(**_fields(value, contributions=contributions))

    def block(value):
        exercises = {key: exercise(ex) for key, ex in value.exercises.items()}
        return CompactBlock.model_construct(**_fields(value, exercises=exercises,
            muscles=[muscle(m) for m in value.muscles]))

    if kind == 'exercise':
        payload = exercise(resource)
    elif kind == 'muscles':
        payload = CompactMuscles.model_construct(**_fields(resource, muscles=[muscle(m) for m in resource.muscles]))
    elif kind == 'block':
        payload = block(resource)
    elif kind == 'comparison':
        payload = CompactComparison.model_construct(**_fields(resource, a=block(resource.a), b=block(resource.b)))
    else:
        raise ValueError('unsupported_compact_resource')
    return AnalyticsCompactV1.model_construct(resource=kind, owner_uid=owner_uid,
        data_revision=resource.data_revision, payload=payload,
        contexts=contexts, metrics=metrics, points=points)
