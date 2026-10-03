from uuid import uuid4
from math import inf, nan

import pytest
from pydantic import ValidationError

from api.schemas.gym_profiles import (
    ActiveGymPayload,
    BoundGymExerciseSetup,
    ExerciseLoadPreferencePayload,
    ExerciseLoadPreferenceView,
    GymStep,
    GymExerciseSetupPayload,
    GymExerciseSetupView,
    GymProfilePayload,
    GymProfileView,
    PlateInventoryItem,
    RevisionedDeletePayload,
    WeightedCount,
)


def test_gym_profile_payload_trims_name_and_accepts_inventory():
    payload = GymProfilePayload(
        name="  Home gym  ",
        equipment=["barbell", "plate"],
        bars=[{"weight": 20, "count": 1, "unit": "kg"}],
        discs=[{"weight": 2.5, "count": 4, "unit": "kg"}],
        steps=[{"category": "plate", "unit": "kg", "value": 1.25}],
        expected_revision=0,
    )
    assert payload.name == "Home gym"
    assert payload.bars[0].count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"name": "   "},
        {"name": "x" * 121},
        {"equipment": ["mystery_machine"]},
        {"bars": [{"weight": 0, "count": 1, "unit": "kg"}]},
        {"discs": [{"weight": 5, "count": -1, "unit": "lb"}]},
        {"discs": [{"weight": 5, "count": 1.5, "unit": "lb"}]},
        {"steps": [{"category": "plate", "unit": "kg", "value": 0}]},
        {"steps": [{"category": "other", "unit": "kg", "value": 1}]},
        {"expected_revision": -1},
    ],
)
def test_gym_profile_payload_rejects_invalid_values(change):
    data = {
        "name": "Home",
        "equipment": ["barbell"],
        "bars": [],
        "discs": [],
        "steps": [],
        "expected_revision": 0,
    }
    data.update(change)
    with pytest.raises(ValidationError):
        GymProfilePayload(**data)


def test_gym_profile_view_includes_id_revision_and_active_state():
    profile_id = uuid4()
    view = GymProfileView(
        id=profile_id,
        name="Home",
        equipment=[],
        bars=[],
        discs=[],
        steps=[],
        revision=2,
        is_active=True,
    )
    assert view.id == profile_id and view.is_active


@pytest.mark.parametrize(
    "data",
    [
        {"step_value": 0},
        {"step_unit": "lbs"},
        {"loading_sides": 3},
        {"base_weight": -1},
        {"weight_basis": "total"},
        {"plate_inventory": [{"weight": 0, "count": 1}]},
        {"plate_inventory": [{"weight": 5, "count": 1.2}]},
    ],
)
def test_setup_payload_rejects_invalid_load_configuration(data):
    payload = {
        "id": uuid4(),
        "is_available": True,
        "is_preferred": False,
        "step_value": 2.5,
        "step_unit": "kg",
        "loading_sides": 2,
        "base_weight": None,
        "weight_basis": "displayed",
        "plate_inventory": None,
        "expected_revision": 0,
    }
    payload.update(data)
    with pytest.raises(ValidationError):
        GymExerciseSetupPayload(**payload)


def test_setup_payload_accepts_canonical_modes_and_inventory():
    stack = GymExerciseSetupPayload(
        id=uuid4(), is_available=True, is_preferred=True, step_value=5,
        step_unit="lb", loading_sides=1, weight_basis="displayed", expected_revision=0,
    )
    plate = GymExerciseSetupPayload(
        id=uuid4(), is_available=True, is_preferred=False, step_value=1.25,
        step_unit="kg", loading_sides=2, base_weight=20,
        weight_basis="including_start_weight",
        plate_inventory=[{"weight": 2.5, "count": 4}], expected_revision=1,
    )
    assert stack.weight_basis == "displayed"
    assert plate.plate_inventory[0].count == 4
    bound = stack.bind_mode("stack")
    assert isinstance(bound, BoundGymExerciseSetup)
    assert bound.mode == "stack" and bound.expected_revision == 0


def test_setup_binding_rejects_basis_that_does_not_match_url_mode():
    setup = GymExerciseSetupPayload(
        id=uuid4(), is_available=True, is_preferred=False, step_value=1.25,
        step_unit="kg", loading_sides=2, weight_basis="displayed", expected_revision=0,
    )
    with pytest.raises(ValidationError):
        setup.bind_mode("plate_loaded")


def test_setup_binding_rejects_unknown_url_mode():
    setup = GymExerciseSetupPayload(
        id=uuid4(), is_available=True, is_preferred=False, step_value=1.25,
        step_unit="kg", loading_sides=2, weight_basis="displayed", expected_revision=0,
    )
    with pytest.raises(ValidationError):
        setup.bind_mode("bogus")


def test_plate_loaded_binding_requires_start_weight_for_including_basis():
    setup = GymExerciseSetupPayload(
        id=uuid4(), is_available=True, is_preferred=False, step_value=1.25,
        step_unit="kg", loading_sides=2, weight_basis="including_start_weight",
        expected_revision=0,
    )
    with pytest.raises(ValidationError):
        setup.bind_mode("plate_loaded")


@pytest.mark.parametrize(
    "factory",
    [
        lambda value: WeightedCount(weight=value, count=0, unit="kg"),
        lambda value: PlateInventoryItem(weight=value, count=0),
        lambda value: GymStep(category="plate", unit="kg", value=value),
        lambda value: GymExerciseSetupPayload(
            id=uuid4(), is_available=True, is_preferred=False, step_value=value,
            step_unit="kg", loading_sides=1, weight_basis="displayed", expected_revision=0,
        ),
        lambda value: GymExerciseSetupPayload(
            id=uuid4(), is_available=True, is_preferred=False, step_value=1,
            step_unit="kg", loading_sides=1, base_weight=value, weight_basis="displayed",
            expected_revision=0,
        ),
    ],
)
@pytest.mark.parametrize("value", [nan, inf, -inf])
def test_numeric_weight_fields_reject_non_finite_values(factory, value):
    with pytest.raises(ValidationError):
        factory(value)


def test_setup_view_adds_exercise_and_mode_url_fields():
    view = GymExerciseSetupView(
        id=uuid4(), gym_id=uuid4(), exercise_source="global", exercise_id=12,
        mode="stack", is_available=True, is_preferred=False, step_value=2.5,
        step_unit="kg", loading_sides=1, base_weight=None, weight_basis="displayed",
        plate_inventory=None, revision=3,
    )
    assert view.exercise_id == 12 and view.revision == 3


@pytest.mark.parametrize(
    "data",
    [
        {"enabled_modes": []},
        {"enabled_modes": ["stack", "stack"]},
        {"enabled_modes": ["stack", "bad"]},
        {"enabled_modes": ["stack"], "preferred_mode": "plate_loaded"},
    ],
)
def test_load_preference_rejects_invalid_mode_sets(data):
    payload = {
        "id": uuid4(), "enabled_modes": ["stack"], "preferred_mode": "stack",
        "expected_revision": 0,
    }
    payload.update(data)
    with pytest.raises(ValidationError):
        ExerciseLoadPreferencePayload(**payload)


def test_load_preference_views_and_active_gym_delete_payloads():
    exercise_id = 17
    view = ExerciseLoadPreferenceView(
        id=uuid4(), exercise_source="user", exercise_id=exercise_id,
        enabled_modes=["stack", "plate_loaded"], preferred_mode="stack", revision=1,
    )
    assert view.exercise_id == exercise_id
    assert ActiveGymPayload(gym_profile_id=None).gym_profile_id is None
    assert ActiveGymPayload(gym_profile_id=uuid4()).gym_profile_id is not None
    assert RevisionedDeletePayload(expected_revision=0).expected_revision == 0
    with pytest.raises(ValidationError):
        RevisionedDeletePayload(expected_revision=-1)
