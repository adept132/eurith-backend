"""Pure helpers for resolving supported exercise loading modes."""

from typing import Literal, Sequence

from api.services.models import ExerciseLoadPreference, GymExerciseSetup

LoadMode = Literal["stack", "plate_loaded"]


def resolve_allowed_modes(
    catalog_equipment: Sequence[str],
    user_preference: ExerciseLoadPreference | None,
    gym_setups: Sequence[GymExerciseSetup],
) -> tuple[LoadMode, ...]:
    """Resolve modes enabled by catalog defaults, user preference, and inventory."""
    if user_preference is not None:
        allowed = set(user_preference.enabled_modes)
    else:
        allowed: set[str] = set()
        if "block_machine" in catalog_equipment:
            allowed.add("stack")
        if "free_machine" in catalog_equipment:
            allowed.add("plate_loaded")
        for setup in gym_setups:
            if setup.deleted_at is None and setup.is_available:
                allowed.add(setup.load_mode)

    return tuple(mode for mode in ("stack", "plate_loaded") if mode in allowed)
