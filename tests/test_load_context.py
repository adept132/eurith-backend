from types import SimpleNamespace
from decimal import Decimal

import pytest

from api.services.load_context import resolve_allowed_modes, resolve_load_context


def setup(mode, *, available=True, deleted_at=None):
    return SimpleNamespace(load_mode=mode, is_available=available, deleted_at=deleted_at)


def test_catalog_block_machine_allows_stack():
    assert resolve_allowed_modes(["block_machine"], None, ()) == ("stack",)


def test_catalog_free_machine_allows_plate_loaded():
    assert resolve_allowed_modes(["free_machine"], None, ()) == ("plate_loaded",)


def test_both_catalog_machine_types_allow_modes_in_stable_order():
    assert resolve_allowed_modes(["free_machine", "block_machine"], None, ()) == (
        "stack", "plate_loaded"
    )


def test_other_catalog_equipment_does_not_imply_a_machine_mode():
    assert resolve_allowed_modes(["barbell", "dumbbell"], None, ()) == ()


def test_explicit_preference_is_authoritative_over_catalog_and_inventory():
    preference = SimpleNamespace(enabled_modes=["plate_loaded"])
    assert resolve_allowed_modes(
        ["block_machine"], preference, [setup("stack")]
    ) == ("plate_loaded",)


def test_active_available_setup_adds_mode_without_explicit_preference():
    assert resolve_allowed_modes(
        ["block_machine"], None, [setup("plate_loaded")]
    ) == ("stack", "plate_loaded")


def test_unavailable_and_archived_setups_do_not_add_modes():
    assert resolve_allowed_modes(
        ["block_machine"], None,
        [setup("plate_loaded", available=False), setup("plate_loaded", deleted_at=object())],
    ) == ("stack",)


def test_duplicate_catalog_and_setup_modes_are_returned_once():
    assert resolve_allowed_modes(
        ["block_machine", "block_machine"], None, [setup("stack"), setup("stack")]
    ) == ("stack",)


def preference(*, enabled=("stack", "plate_loaded"), preferred="plate_loaded"):
    return SimpleNamespace(enabled_modes=list(enabled), preferred_mode=preferred)


def gym(**overrides):
    values = dict(id="gym-1", name="Downtown", steps=[], discs=[])
    values.update(overrides)
    return SimpleNamespace(**values)


def load_setup(mode, **overrides):
    values = dict(
        id=f"{mode}-setup", gym_id="gym-1", load_mode=mode,
        is_available=True, is_preferred=False, deleted_at=None,
        step_value=5, step_unit="lb" if mode == "stack" else "kg",
        loading_sides=1 if mode == "stack" else 2,
        base_weight=None, weight_basis="displayed" if mode == "stack" else "plates_only",
        plate_inventory=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_requested_setup_values_override_gym_values():
    context = resolve_load_context(
        ["block_machine"], preference(), gym(steps=[{"category": "block", "unit": "kg", "value": 10}], discs=[{"weight": 2.5, "count": 4, "unit": "kg"}]),
        [load_setup("plate_loaded", is_preferred=True, step_value=1.25, step_unit="lb", loading_sides=1,
                    base_weight=20, weight_basis="including_start_weight",
                    plate_inventory=[{"weight": 5, "count": 2, "unit": "lb"}])],
        "plate_loaded",
    )
    assert (context.step_value, context.step_unit, context.loading_sides) == (1.25, "lb", 1)
    assert context.base_weight == 20
    assert context.weight_basis == "including_start_weight"
    assert context.plates == ({"weight": 5, "count": 2, "unit": "lb"},)


def test_preferred_active_setup_wins_when_mode_is_not_requested():
    preferred = load_setup("plate_loaded", is_preferred=True, id="preferred")
    other = load_setup("stack", id="other")
    context = resolve_load_context(
        ["block_machine"], preference(preferred="stack"), gym(), [other, preferred], None
    )
    assert context is not None
    assert (context.mode, context.setup_id) == ("plate_loaded", "preferred")


def test_requested_allowed_mode_without_gym_setup_is_resolved_once():
    context = resolve_load_context(["block_machine"], preference(enabled=("stack",)), gym(), [], "stack")
    assert context is not None
    assert context.setup_id is None
    assert context.mode == "stack"


def test_named_gym_without_discs_keeps_plate_inventory_unknown():
    context = resolve_load_context(
        ["free_machine"], preference(enabled=("plate_loaded",)), gym(), [], "plate_loaded"
    )
    assert context is not None
    assert context.plates is None


def test_legacy_global_settings_supply_steps_and_plates():
    context = resolve_load_context(
        ["free_machine"], preference(enabled=("plate_loaded",)), None, [], "plate_loaded",
        global_settings={"weight_steps": {"plate_lb": 5}, "plate_config_lbs": [{"weight": 10, "count": 2}]},
    )
    assert context is not None
    assert (context.step_value, context.step_unit) == (5, "lb")
    assert context.plates == ({"weight": 10, "count": 2, "unit": "lb"},)


@pytest.mark.parametrize(
    ("step_key", "config_key", "unit"),
    [("plate_kg", "plate_config_kg", "kg"), ("plate_lb", "plate_config_lbs", "lb")],
)
def test_mobile_plate_config_object_supplies_frozen_global_inventory(step_key, config_key, unit):
    plates = [{"weight": 2.5, "count": 4, "metadata": {"label": "saved"}}]
    config = {"barWeights": [20], "selectedBar": 20, "plates": plates}
    context = resolve_load_context(
        ["free_machine"], preference(enabled=("plate_loaded",)), None, [], "plate_loaded",
        global_settings={"weight_steps": {step_key: 2.5}, config_key: config},
    )
    assert context is not None
    assert context.step_unit == unit
    assert context.plates == ({"weight": 2.5, "count": 4, "unit": unit,
                               "metadata": {"label": "saved"}},)
    plates[0]["count"] = 0
    plates[0]["metadata"]["label"] = "changed"
    assert context.plates[0]["count"] == 4
    assert context.plates[0]["metadata"]["label"] == "saved"
    with pytest.raises(TypeError):
        context.plates[0]["metadata"]["label"] = "changed"


@pytest.mark.parametrize("invalid_plates", [None, {}, "plates", 7, ["bad"]])
def test_invalid_mobile_plate_config_has_no_snapshot_inventory(invalid_plates):
    context = resolve_load_context(
        ["free_machine"], preference(enabled=("plate_loaded",)), None, [], "plate_loaded",
        global_settings={"plate_config_kg": {"barWeights": [20], "selectedBar": 20,
                                              "plates": invalid_plates}},
    )
    assert context is not None
    assert context.plates is None


def test_named_gym_without_discs_does_not_use_mobile_global_plate_config():
    context = resolve_load_context(
        ["free_machine"], preference(enabled=("plate_loaded",)), gym(discs=None), [],
        "plate_loaded", global_settings={"plate_config_kg": {
            "barWeights": [20], "selectedBar": 20,
            "plates": [{"weight": 2.5, "count": 4}],
        }},
    )
    assert context is not None
    assert context.plates is None


def test_pound_stack_step_preserves_its_unit():
    context = resolve_load_context(
        ["block_machine"], preference(enabled=("stack",)), gym(steps=[{"category": "block", "unit": "lb", "value": 5}]), [], "stack"
    )
    assert context is not None
    assert (context.step_value, context.step_unit) == (5, "lb")


def test_invalid_setup_weight_basis_returns_none():
    context = resolve_load_context(
        ["free_machine"], preference(enabled=("plate_loaded",)), gym(),
        [load_setup("plate_loaded", weight_basis="including_start_weight", base_weight=None)], "plate_loaded",
    )
    assert context is None


def test_resolved_context_is_snapshot_of_json_configuration():
    steps = [{"category": "block", "unit": "kg", "value": 7.5}]
    discs = [{"weight": 2.5, "count": 2, "unit": "kg"}]
    location = gym(steps=steps, discs=discs)
    context = resolve_load_context(
        ["block_machine"], preference(enabled=("stack",)), location, [], "stack"
    )
    steps[0]["value"] = 99
    discs[0]["count"] = 0
    assert context is not None
    assert context.step_value == 7.5
    assert context.plates == ({"weight": 2.5, "count": 2, "unit": "kg"},)


def test_decimal_setup_step_and_base_are_valid_numeric_values():
    context = resolve_load_context(
        ["free_machine"], preference(enabled=("plate_loaded",)), gym(),
        [load_setup("plate_loaded", step_value=Decimal("1.25"),
                    weight_basis="including_start_weight", base_weight=Decimal("20.0"))],
        "plate_loaded",
    )
    assert context is not None
    assert (context.step_value, context.base_weight) == (1.25, 20.0)


def test_non_null_setup_with_empty_weight_basis_is_invalid():
    context = resolve_load_context(
        ["free_machine"], preference(enabled=("plate_loaded",)), gym(),
        [load_setup("plate_loaded", weight_basis="")], "plate_loaded",
    )
    assert context is None


def test_plate_snapshot_rejects_mutation_and_remains_json_serializable():
    import json

    context = resolve_load_context(
        ["free_machine"], preference(enabled=("plate_loaded",)),
        gym(discs=[{"weight": 2.5, "count": 2, "unit": "kg"}]), [], "plate_loaded",
    )
    assert context is not None
    with pytest.raises(TypeError):
        context.plates[0]["count"] = 99
    assert json.loads(json.dumps(context.plates)) == [{"weight": 2.5, "count": 2, "unit": "kg"}]
