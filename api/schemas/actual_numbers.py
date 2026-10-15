"""Actual-result storage format, independent of analytics eligibility.

PostgreSQL sets use INTEGER reps and NUMERIC(8,2) kilograms. These are
serialization/storage limits, never physiological or e1RM exclusions.
"""
from decimal import Decimal, InvalidOperation
from typing import Annotated
from pydantic import BeforeValidator


def actual_reps(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('actual_reps_format: finite nonnegative INTEGER required') from error
    if isinstance(value, bool) or not number.is_finite() or number < 0 or number != number.to_integral_value() or number > 2147483647:
        raise ValueError('actual_reps_format: finite nonnegative PostgreSQL INTEGER required')
    return int(number)


def actual_weight(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('actual_weight_format: finite nonnegative NUMERIC(8,2) required') from error
    if isinstance(value, bool) or not number.is_finite() or not 0 <= number <= Decimal('999999.99'):
        raise ValueError('actual_weight_format: finite nonnegative PostgreSQL NUMERIC(8,2) required')
    return number


ActualReps = Annotated[int, BeforeValidator(actual_reps)]
ActualWeight = Annotated[Decimal, BeforeValidator(actual_weight)]
