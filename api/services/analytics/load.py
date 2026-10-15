"""Normalize explicit historical load snapshots without inventing missing facts."""
from dataclasses import fields
from datetime import date
import json
import math
from .policy import EFFORT_TO_RIR, finite_number, positive_reps
from .types import RawSetFact, NormalizedSetFact


def lombardi(weight_kg: float, reps: int) -> float | None:
    weight = finite_number(weight_kg)
    if weight is None or weight <= 0 or not positive_reps(reps):
        return None
    try:
        result = weight * reps ** 0.1
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def normalize_set(raw: RawSetFact) -> NormalizedSetFact:
    context = raw.load_context or {}
    reasons = []
    weight = None
    repetitions = raw.reps if positive_reps(raw.reps) else None
    if repetitions is None:
        reasons.append('invalid_reps')
    model, basis = context.get('model'), context.get('input_basis')
    recorded = finite_number(raw.weight_kg)
    if context.get('schema_version') != 1:
        reasons.append('unknown_load_context')
    elif model == 'partial_bodyweight':
        reasons.append('partial_bodyweight_model_unavailable')
    elif model == 'bodyweight' and basis == 'body_components':
        mass = (raw.session_context or {}).get('body_mass') or {}
        body = finite_number(mass.get('weight_kg'))
        added = finite_number(context.get('added_load_kg'))
        assistance = finite_number(context.get('assistance_kg'))
        measured_on = mass.get('measured_on')
        try:
            measured_date = date.fromisoformat(measured_on) if isinstance(measured_on,str) else measured_on
            future_mass = measured_date is not None and measured_date > raw.started_at.date()
        except (ValueError, TypeError):
            future_mass = True
        if future_mass:
            reasons.append('future_body_mass')
        elif body is None or body <= 0:
            reasons.append('unknown_body_mass')
        elif added is None or assistance is None:
            reasons.append('unknown_body_components')
        elif added < 0 or assistance < 0:
            reasons.append('invalid_body_components')
        else:
            weight = body + added - assistance
    elif model in {'barbell','dumbbell','stack','plate_loaded'}:
        if recorded is None or recorded < 0:
            reasons.append('invalid_weight')
        elif basis in {'full','displayed'}:
            weight = recorded
        elif basis == 'plates_only':
            base = finite_number(context.get('base_weight_kg'))
            if base is None:
                reasons.append('unknown_base_weight')
            elif base < 0:
                reasons.append('invalid_base_weight')
            else:
                weight = base + recorded
        elif basis == 'per_implement' and model == 'dumbbell':
            weight = recorded
        else:
            reasons.append('unknown_input_basis')
    else:
        reasons.append('unknown_load_model')
    e1rm_reps = int(repetitions) if repetitions is not None else None
    tonnage_reps = repetitions
    if model == 'dumbbell' and basis == 'per_implement':
        rep_basis = context.get('rep_basis')
        if rep_basis == 'simultaneous':
            count = context.get('implement_count')
            if positive_reps(count) and repetitions is not None:
                tonnage_reps = repetitions * count
            else:
                tonnage_reps = None
                reasons.append('unknown_implement_count')
        elif rep_basis == 'per_side':
            left, right = context.get('left_reps'), context.get('right_reps')
            if positive_reps(left) and positive_reps(right):
                tonnage_reps = left + right
            else:
                tonnage_reps = None
                reasons.append('unknown_side_reps')
        elif rep_basis != 'total':
            tonnage_reps = None
            reasons.append('unknown_rep_basis')
    if model == 'dumbbell' and basis == 'per_implement' and context.get('rep_basis') in {'per_side','total'}:
        left, right = context.get('left_reps'), context.get('right_reps')
        if positive_reps(left) and positive_reps(right) and left == right:
            e1rm_reps = int(left)
        else:
            e1rm_reps = None
            reasons.append('ambiguous_e1rm_reps')
    if weight is not None and (not math.isfinite(weight) or weight <= 0):
        weight = None
        reasons.append('nonpositive_load')
    tonnage = weight * tonnage_reps if weight is not None and tonnage_reps is not None else None
    if tonnage is not None and not math.isfinite(tonnage):
        tonnage = None
        reasons.append('nonfinite_tonnage')
    # Provenance and apparatus identity do not split the exercise history.
    comparison_context = {k:context.get(k) for k in ('schema_version','model',
        'input_basis','base_weight_kg','implement_count','rep_basis',
        'added_load_kg','assistance_kg','model_id')}
    comparison_context['body_mass_kg'] = ((raw.session_context or {}).get('body_mass') or {}).get('weight_kg')
    for key in ('base_weight_kg','implement_count','added_load_kg','assistance_kg','body_mass_kg'):
        comparison_context[key] = finite_number(comparison_context[key])
    comparison_key = json.dumps(comparison_context, sort_keys=True, separators=(',',':'), default=str)
    return NormalizedSetFact(**{f.name:getattr(raw,f.name) for f in fields(RawSetFact)},
        e1rm_weight_kg=weight, tonnage_kg=tonnage,
        rir=EFFORT_TO_RIR.get(raw.effort_level), comparison_key=comparison_key,
        unavailable=tuple(reasons), e1rm_reps=e1rm_reps)
