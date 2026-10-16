"""Context transport and semantic comparisons; transaction owners commit/mark dirty."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from api.schemas.analytics_context import BodyMassSnapshot
from api.services.models import WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet
from .config import analytics_enabled
from .snapshots import capture_workout_snapshot, append_workout_weighing


def json_value(value):
    return deepcopy(value.model_dump(mode='json') if hasattr(value, 'model_dump') else value)


def apply_field(target, payload, name):
    if name not in payload.model_fields_set:
        return
    value = json_value(getattr(payload, name))
    if value != getattr(target, name) and value is not None and not analytics_enabled():
        raise HTTPException(409, {'error': 'analytics_disabled'})
    setattr(target, name, value)


def apply_set_context(target, payload, exercise, *, is_new=False):
    current = target.analytics_load_snapshot
    if (not is_new and current and current.get('input_basis') == 'body_components'
            and 'weight' in payload.model_fields_set and payload.weight != target.weight
            and 'analytics_load_snapshot' not in payload.model_fields_set):
        raise HTTPException(409, {'error': 'analytics_context_required',
                                  'set_id': target.id, 'weight': str(target.weight) if target.weight is not None else None,
                                  'reps': target.reps,
                                  'analytics_load_snapshot': deepcopy(current)})
    if is_new and ('analytics_load_snapshot' not in payload.model_fields_set
                   or json_value(payload.analytics_load_snapshot) == exercise.analytics_load_defaults):
        # Reusing the established exercise mode is an ordinary set addition.
        target.analytics_load_snapshot = deepcopy(exercise.analytics_load_defaults)
    else:
        apply_field(target, payload, 'analytics_load_snapshot')
    if 'anomaly_review' in payload.model_fields_set:
        from api.services.anomaly_guard import review_input_state
        supplied = json_value(payload.anomaly_review)
        # A full client replay may omit the server's derived reasons/timestamp.
        # Only a changed confirmation or non-derived annotation is a new input.
        if review_input_state(supplied) != review_input_state(target.anomaly_review):
            apply_field(target, payload, 'anomaly_review')


async def apply_session_context(db, workout, payload, *, is_new=False):
    previous_context = deepcopy(workout.analytics_snapshot)
    intent = getattr(payload, 'analytics_mass_intent', None)
    established_mass = deepcopy((workout.analytics_snapshot or {}).get('body_mass') or {})
    established_replay = intent is not None and (
        established_mass.get('measurement_client_uuid') == intent.client_uuid
        and established_mass.get('weight_kg') == intent.weight_kg
        and established_mass.get('measured_on') == intent.measured_on.isoformat())
    if 'analytics_snapshot' in payload.model_fields_set:
        supplied = payload.analytics_snapshot
        value = json_value(supplied)
        # Offline full snapshots can still carry the original manual mass.
        # The canonical journal acknowledgement, including capture time, wins on replay.
        if (established_replay and supplied and supplied.body_mass
                and supplied.body_mass.weight_kg == intent.weight_kg
                and supplied.body_mass.measured_on == intent.measured_on):
            value['body_mass'] = established_mass
        if value != workout.analytics_snapshot and value is not None and not analytics_enabled():
            raise HTTPException(409, {'error': 'analytics_disabled'})
        # Validate owner-linked mass even when caller merely substitutes a snapshot.
        if supplied is not None:
            canonical = await capture_workout_snapshot(db, workout, explicit_mass=supplied.body_mass)
            # Phase belongs to the saved server context, never optional client defaults.
            value = canonical.model_dump(mode='json')
            if 'body_mass' in supplied.model_fields_set:
                value['body_mass'] = json_value(supplied.body_mass)
            if established_replay:
                value['body_mass'] = established_mass
        workout.analytics_snapshot = value
    if intent is not None:
        old = (workout.analytics_snapshot or {}).get('body_mass') or {}
        replay = (old.get('measurement_client_uuid') == intent.client_uuid
                  and old.get('weight_kg') == intent.weight_kg
                  and old.get('measured_on') == intent.measured_on.isoformat())
        if not replay and not analytics_enabled():
            raise HTTPException(409, {'error': 'analytics_disabled'})
        # Capture validates the profile-local start cutoff BEFORE appending.
        mass = BodyMassSnapshot(weight_kg=intent.weight_kg, measured_on=intent.measured_on,
                                source='manual', captured_at=datetime.now(timezone.utc))
        if not replay:
            await capture_workout_snapshot(db, workout, explicit_mass=mass)
        else:
            await capture_workout_snapshot(db, workout,
                explicit_mass=BodyMassSnapshot.model_validate(old))
        measurement_id = await append_workout_weighing(db, workout.app_user_id,
            client_uuid=intent.client_uuid, weight_kg=Decimal(str(intent.weight_kg)),
            measured_on=intent.measured_on)
        # Keep capture time stable for idempotent replay.
        if not replay:
            mass = mass.model_copy(update={'source': 'journal', 'measurement_id': measurement_id,
                                           'measurement_client_uuid': intent.client_uuid})
            await capture_workout_snapshot(db, workout, explicit_mass=mass)
    elif is_new and analytics_enabled() and workout.analytics_snapshot is None:
        await capture_workout_snapshot(db, workout, explicit_mass=None)

    if previous_context != workout.analytics_snapshot:
        from api.services.anomaly_stats import review_workout
        await review_workout(db, workout)


async def fingerprint(db, workout_id):
    """Persisted semantic state, excluding version and transport timestamps."""
    await db.flush()
    result = []
    for model, predicate in (
        (WorkoutSession, WorkoutSession.id == workout_id),
        (WorkoutSessionExercise, WorkoutSessionExercise.workout_session_id == workout_id),
        (WorkoutSessionSet, WorkoutSessionSet.workout_session_exercise_id.in_(
            select(WorkoutSessionExercise.id).where(WorkoutSessionExercise.workout_session_id == workout_id))),
    ):
        columns = [c for c in model.__table__.columns if c.name not in
                   {'sync_version', 'updated_at', 'created_at', 'session_rpe_at'}]
        rows = (await db.execute(select(*columns).where(predicate).order_by(model.id))).all()
        result.append(deepcopy([tuple(row) for row in rows]))
    return result
