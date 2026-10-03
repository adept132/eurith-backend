"""Pure helpers for resolving supported exercise loading modes."""

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import math
from typing import Any, Literal, Sequence

from api.services.models import ExerciseLoadPreference, GymExerciseSetup

LoadMode = Literal["stack", "plate_loaded"]


class _FrozenDict(dict):
    """JSON-serializable dict that rejects mutation after construction."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("plate snapshots are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True)
class EffectiveLoadContext:
    gym_id: Any | None
    gym_name: str | None
    setup_id: Any | None
    mode: LoadMode
    step_value: float
    step_unit: str
    loading_sides: int
    weight_basis: str
    base_weight: float | None
    plates: tuple[dict[str, Any], ...] | None


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


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _positive_number(value: Any) -> bool:
    if isinstance(value, Decimal):
        return value.is_finite() and value > 0
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _copy_plates(inventory: Any, default_unit: str) -> tuple[dict[str, Any], ...] | None:
    if inventory is None:
        return None
    if isinstance(inventory, dict):
        inventory = inventory.get("plates")
    if not isinstance(inventory, list) or not inventory:
        return None
    result = []
    for plate in inventory:
        if not isinstance(plate, dict) or not _positive_number(plate.get("weight")):
            continue
        count = plate.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            continue
        item = deepcopy(plate)
        item.setdefault("unit", default_unit)
        if item["unit"] not in ("kg", "lb"):
            continue
        result.append(_freeze_json(item))
    return tuple(result) if result else None


def _gym_step(gym: Any, mode: LoadMode) -> tuple[float, str] | None:
    category = "block" if mode == "stack" else "plate"
    for item in _get(gym, "steps", []) or []:
        if (isinstance(item, dict) and item.get("category") == category
                and item.get("unit") in ("kg", "lb") and _positive_number(item.get("value"))):
            return float(item["value"]), item["unit"]
    return None


def resolve_load_context(
    catalog_equipment: Sequence[str],
    user_preference: ExerciseLoadPreference | None,
    gym: Any | None,
    gym_setups: Sequence[GymExerciseSetup],
    requested_mode: str | None,
    global_settings: dict[str, Any] | None = None,
) -> EffectiveLoadContext | None:
    """Resolve one immutable-in-practice snapshot of the effective loading setup."""
    setups = [s for s in gym_setups if _get(s, "deleted_at") is None and _get(s, "is_available")]
    if gym is not None:
        gym_id = _get(gym, "id")
        setups = [s for s in setups if _get(s, "gym_id", gym_id) == gym_id]
    allowed = resolve_allowed_modes(catalog_equipment, user_preference, setups)
    if requested_mode is not None:
        if requested_mode not in allowed:
            return None
        mode: LoadMode = requested_mode  # type: ignore[assignment]
    else:
        selected = next((s for s in setups if _get(s, "is_preferred") and _get(s, "load_mode") in allowed), None)
        selected = selected or next((s for s in setups if _get(s, "load_mode") in allowed), None)
        preferred = _get(user_preference, "preferred_mode")
        if selected is not None:
            mode = _get(selected, "load_mode")
        elif preferred in allowed:
            mode = preferred
        elif "stack" in allowed:
            mode = "stack"
        elif "plate_loaded" in allowed:
            mode = "plate_loaded"
        else:
            return None
    setup = next((s for s in setups if _get(s, "load_mode") == mode), None)

    safe_step = (10.0, "lb") if mode == "stack" else (2.5, "kg")
    step = None
    if setup is not None and _positive_number(_get(setup, "step_value")) and _get(setup, "step_unit") in ("kg", "lb"):
        step = (float(_get(setup, "step_value")), _get(setup, "step_unit"))
    if step is None and gym is not None:
        step = _gym_step(gym, mode)
    if step is None and gym is None and global_settings:
        settings_steps = global_settings.get("weight_steps") or {}
        keys = ("block_lb",) if mode == "stack" else ("plate_kg", "plate_lb")
        for key in keys:
            if _positive_number(settings_steps.get(key)):
                step = (float(settings_steps[key]), "lb" if key.endswith("lb") else "kg")
                break
    step = step or safe_step
    defaults = (1, "displayed") if mode == "stack" else (2, "plates_only")
    sides = _get(setup, "loading_sides") if setup is not None else None
    sides = sides if isinstance(sides, int) and sides in (1, 2) else None
    gym_sides = _get(gym, "loading_sides")
    if not isinstance(gym_sides, int) or isinstance(gym_sides, bool) or gym_sides not in (1, 2):
        gym_sides = None
    sides = sides or gym_sides or defaults[0]
    if setup is not None:
        basis = _get(setup, "weight_basis")
    else:
        basis = _get(gym, "weight_basis") or defaults[1]
    base = _get(setup, "base_weight") if setup is not None else None
    base = base if base is not None else _get(gym, "base_weight")
    if mode == "stack":
        if basis != "displayed":
            return None
    elif basis not in ("plates_only", "including_start_weight"):
        return None
    if basis == "including_start_weight" and (
        base is None
        or not isinstance(base, (int, float, Decimal))
        or isinstance(base, bool)
        or (not base.is_finite() if isinstance(base, Decimal) else not math.isfinite(base))
        or base < 0
    ):
        return None

    inventory = _get(setup, "plate_inventory") if setup is not None else None
    if inventory is None and gym is not None:
        inventory = _get(gym, "discs")
    if inventory is None and gym is None and global_settings:
        config_key = "plate_config_kg" if step[1] == "kg" else "plate_config_lbs"
        inventory = global_settings.get(config_key)
    plates = _copy_plates(inventory, step[1])
    return EffectiveLoadContext(
        gym_id=_get(gym, "id") if gym is not None else None,
        gym_name=_get(gym, "name") if gym is not None else None,
        setup_id=_get(setup, "id") if setup is not None else None,
        mode=mode,
        step_value=step[0],
        step_unit=step[1],
        loading_sides=sides,
        weight_basis=basis,
        base_weight=float(base) if base is not None else None,
        plates=plates,
    )
