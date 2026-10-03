from types import SimpleNamespace as Row

from api.services.exercise_selection_engine import SelectionConfig, select_exercises
from api.services.gym_selection import eligible_for_gym


def exercise(equipment, *, id=1, source="global"):
    return Row(id=id, exercise_source=source, equipment_needed=equipment)


def gym(*equipment):
    return Row(id="gym-one", equipment=list(equipment))


def setup(mode="stack", **changes):
    values = dict(id="setup-one", gym_id="gym-one", exercise_source="global",
                  exercise_id=1, load_mode=mode, is_available=True,
                  is_preferred=False, deleted_at=None)
    values.update(changes)
    return Row(**values)


def test_non_machine_requirements_are_all_required_and_normalized():
    ex = exercise(["Скамья", "Гантели"])
    result = eligible_for_gym(ex, gym("dumbbell"), [], ())
    assert (result.eligible, result.reason_code) == (False, "missing_equipment")
    assert eligible_for_gym(ex, gym("bench", "dumbbell"), [], ()).eligible
    assert eligible_for_gym(exercise([]), gym(), [], ()).eligible


def test_machine_modes_are_alternatives_but_other_equipment_is_required():
    ex = exercise(["bench", "block_machine", "free_machine"])
    assert eligible_for_gym(ex, gym("block_machine"), [setup()], ("stack", "plate_loaded")).reason_code == "missing_equipment"
    result = eligible_for_gym(ex, gym("bench"), [setup("plate_loaded")], ("stack", "plate_loaded"))
    assert (result.eligible, result.selected_mode, result.setup_id) == (True, "plate_loaded", "setup-one")


def test_broad_machine_inventory_does_not_replace_setup():
    result = eligible_for_gym(exercise(["block_machine"]), gym("block_machine"), [], ("stack",))
    assert (result.eligible, result.reason_code) == (False, "no_machine_setup")


def test_only_exact_active_available_setup_matches():
    wrong = [setup(gym_id="other"), setup(exercise_id=2), setup(exercise_source="user"),
             setup(deleted_at="archived"), setup(is_available=False)]
    assert eligible_for_gym(exercise(["block_machine"]), gym(), wrong, ("stack",)).reason_code == "no_machine_setup"
    assert eligible_for_gym(exercise(["block_machine"], source="user"), gym(), [setup()], ("stack",)).reason_code == "no_machine_setup"


def test_preference_excludes_setup_and_explicit_plate_setup_overrides_catalog_default():
    ex = exercise(["block_machine"])
    assert eligible_for_gym(ex, gym(), [setup("stack")], ("plate_loaded",)).reason_code == "no_allowed_variant"
    result = eligible_for_gym(ex, gym(), [setup("plate_loaded")], ("stack", "plate_loaded"))
    assert (result.eligible, result.selected_mode) == (True, "plate_loaded")


def test_preferred_setup_and_fallback_are_deterministic():
    ex = exercise(["block_machine", "free_machine"])
    rows = [setup("plate_loaded", id="z"), setup("stack", id="a", is_preferred=True)]
    assert eligible_for_gym(ex, gym(), rows, ("plate_loaded", "stack")).selected_mode == "stack"
    rows[1].is_preferred = False
    assert eligible_for_gym(ex, gym(), list(reversed(rows)), ("plate_loaded", "stack")).selected_mode == "stack"


def test_favorite_cannot_bypass_eligibility_and_disliked_is_excluded():
    from api.services.exercise_pattern_tags import ExerciseAction
    def candidate(id, equipment):
        return Row(id=id, exercise_source="global", name=f"ex{id}", equipment_needed=equipment,
                   action=ExerciseAction.push, fatigue_tier=2, main_muscle_group="Грудь",
                   secondary_muscle_groups=[], category="Базовое", vector=None)
    pool = [candidate(1, ["bench"]), candidate(2, []), candidate(3, [])]
    selected = select_exercises({"chest": 3}, pool, None, [],
                                SelectionConfig(seed=1, favorite_exercise_ids={1}, disliked_exercise_ids={2}),
                                gym_eligibility=lambda ex: eligible_for_gym(ex, gym(), [], ()).eligible)
    assert [item.exercise_id for item in selected] == [3]


def test_legacy_equipment_filter_still_applies_without_named_gym():
    from api.services.exercise_pattern_tags import ExerciseAction
    pool = [Row(id=1, name="bench", equipment_needed=["bench"],
                action=ExerciseAction.push, fatigue_tier=2, main_muscle_group="Грудь",
                secondary_muscle_groups=[], category="Базовое", vector=None)]
    config = SelectionConfig(seed=1)
    assert select_exercises({"chest": 3}, pool, {"dumbbell"}, [], config) == []
    selected = select_exercises({"chest": 3}, pool, {"dumbbell"}, [], config,
                                gym_eligibility=lambda ex: eligible_for_gym(ex, gym("bench"), [], ()).eligible)
    assert [item.exercise_id for item in selected] == [1]
