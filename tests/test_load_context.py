from types import SimpleNamespace

from api.services.load_context import resolve_allowed_modes


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
