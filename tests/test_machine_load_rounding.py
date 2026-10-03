"""Reachable machine weights use the actual frozen setup, not a generic grid."""

from dataclasses import replace
import math

from api.services.load_context import EffectiveLoadContext
from api.services.progression.rounding import round_for_load_context


def context(mode="stack", **overrides):
    value = EffectiveLoadContext(
        gym_id="gym", gym_name="Gym", setup_id="setup", mode=mode,
        step_value=5, step_unit="lb", loading_sides=1 if mode == "stack" else 2,
        weight_basis="displayed" if mode == "stack" else "plates_only",
        base_weight=None, plates=None,
    )
    return replace(value, **overrides)


def test_five_pound_stack_uses_pound_grid_without_kg_drift():
    load = context()
    assert math.isclose(round_for_load_context(7, load, True), 6.80388555, abs_tol=0.00001)
    assert math.isclose(round_for_load_context(7, load, False), 6.80388555, abs_tol=0.00001)
    assert round_for_load_context(2, load, True) is None


def test_two_sided_inventory_respects_pairs_and_reachable_weights():
    load = context("plate_loaded", plates=(
        {"weight": 5, "count": 2, "unit": "kg"},
        {"weight": 1.25, "count": 2, "unit": "kg"},
    ))
    assert round_for_load_context(11, load, True) == 10
    assert round_for_load_context(12.5, load, True) == 12.5
    assert round_for_load_context(11, load, False) == 10
    assert round_for_load_context(12, load, False) == 12.5


def test_inventory_missing_or_without_pairs_has_no_fabricated_target():
    assert round_for_load_context(11, context("plate_loaded"), True) is None
    assert round_for_load_context(11, context("plate_loaded", plates=()), False) is None
    lone = context("plate_loaded", plates=({"weight": 5, "count": 1, "unit": "kg"},))
    assert round_for_load_context(11, lone, True) is None


def test_one_sided_and_weight_basis_include_base_only_when_configured():
    load = context("plate_loaded", loading_sides=1, plates=({"weight": 5, "count": 2, "unit": "kg"},))
    assert round_for_load_context(6, load, True) == 5
    assert round_for_load_context(6, replace(load, weight_basis="including_start_weight", base_weight=20), True) is None
    assert round_for_load_context(26, replace(load, weight_basis="including_start_weight", base_weight=20), True) == 25
    assert round_for_load_context(20, replace(load, weight_basis="including_start_weight", base_weight=20), True) == 20


def test_pound_plates_convert_from_inventory_unit_without_kg_grid():
    load = context("plate_loaded", plates=({"weight": 5, "count": 2, "unit": "lb"},))
    assert math.isclose(round_for_load_context(5, load, True), 4.5359237, abs_tol=0.000001)
    assert round_for_load_context(4, load, True) is None


def test_invalid_targets_and_missing_base_return_no_target():
    load = context("plate_loaded", plates=({"weight": 5, "count": 2, "unit": "kg"},))
    for value in (0, -1, math.inf, math.nan):
        assert round_for_load_context(value, load, True) is None
    assert round_for_load_context(30, replace(load, weight_basis="including_start_weight"), True) is None
    assert round_for_load_context(30, replace(load, plates=({"weight": None, "count": 2, "unit": "kg"},)), True) is None
