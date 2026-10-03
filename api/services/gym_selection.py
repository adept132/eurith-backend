"""Pure equipment and setup eligibility for exercises in a named gym."""

from dataclasses import dataclass
from typing import Any, Iterable

from api.services import equipment


_MACHINE_EQUIPMENT = {equipment.BLOCK_MACHINE, equipment.FREE_MACHINE}
_MODE_ORDER = {"stack": 0, "plate_loaded": 1}


@dataclass(frozen=True)
class GymEligibility:
    eligible: bool
    reason_code: str
    selected_mode: str | None = None
    setup_id: Any | None = None


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _codes(items: Iterable[str] | None) -> set[str]:
    if isinstance(items, str):
        items = [items]
    return {
        equipment.normalize_equipment(item) or str(item).strip().lower()
        for item in (items or []) if item and str(item).strip()
    }


def eligible_for_gym(exercise: Any, gym_inventory: Any,
                     exercise_setups: Iterable[Any], allowed_modes: Iterable[str]) -> GymEligibility:
    """Decide whether one exercise can be performed using this gym's equipment.

    Machine categories in the catalog are alternate loading modes. A named gym
    requires an active exercise-specific setup for either mode; broad inventory
    categories alone never establish machine availability.
    """
    required = _codes(_get(exercise, "equipment_needed"))
    if not required:
        required = {equipment.BODYWEIGHT}
    available = _codes(_get(gym_inventory, "equipment", gym_inventory))
    available.add(equipment.BODYWEIGHT)
    if required - _MACHINE_EQUIPMENT - available:
        return GymEligibility(False, "missing_equipment")
    if not required & _MACHINE_EQUIPMENT:
        return GymEligibility(True, "eligible")

    source = _get(exercise, "exercise_source")
    if source is None:
        source = "user" if _get(exercise, "app_user_id") is not None else "global"
    gym_id = _get(gym_inventory, "id")
    setups = [
        row for row in exercise_setups
        if _get(row, "gym_id") == gym_id
        and _get(row, "exercise_source") == source
        and _get(row, "exercise_id") == _get(exercise, "id")
        and _get(row, "deleted_at") is None
        and _get(row, "is_available") is True
        and _get(row, "load_mode") in _MODE_ORDER
    ]
    if not setups:
        return GymEligibility(False, "no_machine_setup")
    allowed = set(allowed_modes or ())
    candidates = [row for row in setups if _get(row, "load_mode") in allowed]
    if not candidates:
        return GymEligibility(False, "no_allowed_variant")
    selected = min(candidates, key=lambda row: (
        not _get(row, "is_preferred", False),
        _MODE_ORDER[_get(row, "load_mode")],
        str(_get(row, "id")),
    ))
    return GymEligibility(True, "eligible", _get(selected, "load_mode"), _get(selected, "id"))
