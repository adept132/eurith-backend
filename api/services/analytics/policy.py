"""Metric admission and numerical comparison, independent of equipment steps."""
import math
from .types import RawSetFact

RULES_VERSION = 'p1-08-v1'
FORMULA_ID = 'lombardi-v1'
LOAD_SCHEMA_VERSION = 1
EFFORT_TO_RIR = {'light':3., 'easy':3., 'medium':2., 'prefailure':1.,
                 'failure':0., 'warmup':4., 'warmup_effort':4.}


def finite_number(value) -> float | None:
    if value is None or isinstance(value, (bool, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def positive_reps(value) -> bool:
    number = finite_number(value)
    return number is not None and number > 0 and number.is_integer()


def strictly_greater(a: float, b: float) -> bool:
    a, b = finite_number(a), finite_number(b)
    return (a is not None and b is not None
            and a - b > max(1e-8, 1e-12 * max(abs(a), abs(b))))


def eligible(raw: RawSetFact, metric: str) -> bool:
    if (not raw.is_completed or raw.finished_at is None or raw.is_anomalous
            or raw.set_type not in {'normal', 'drop'}):
        return False
    if metric in {'drop_sets', 'work_sets'}:
        return raw.set_type == ('drop' if metric == 'drop_sets' else 'normal')
    if metric == 'reps':
        return positive_reps(raw.reps)
    if metric not in {'e1rm', 'pr', 'tonnage', 'intensity', 'rir'}:
        return False
    if raw.set_type == 'drop' and metric != 'tonnage':
        return False
    if metric == 'rir':
        return raw.effort_level in EFFORT_TO_RIR
    # Import locally to keep policy usable without a load-module cycle.
    from .load import normalize_set
    normalized = normalize_set(raw)
    if metric == 'tonnage':
        return normalized.tonnage_kg is not None
    return normalized.e1rm_weight_kg is not None and positive_reps(normalized.e1rm_reps)
