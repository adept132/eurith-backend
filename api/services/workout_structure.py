from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field


WorkoutStructureSetKind = Literal["normal", "amrap", "backoff"]


class WorkoutStructureExerciseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise_id: int
    order_index: int
    superset_group: str | None = None
    target_sets: int = Field(ge=0)
    set_kinds: list[WorkoutStructureSetKind]
    rep_min: int | None = None
    rep_max: int | None = None
    target_rir: int | None = None
    rest_seconds: int | None = None
    notes: str | None = None


class WorkoutStructureDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    notes: str | None = None
    exercises: list[WorkoutStructureExerciseDraft]


_MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _normalized_note(value: str | None) -> str | None:
    trimmed = value.strip() if value else ""
    return trimmed or None


def _default_name(started_at: datetime) -> str:
    return f"Тренировка {started_at.day} {_MONTHS_GENITIVE[started_at.month - 1]}"


def _completed_working_sets(exercise: Any) -> list[Any]:
    return sorted(
        (
            item
            for item in (_attr(exercise, "sets", []) or [])
            if _attr(item, "is_completed", False)
            and _attr(item, "set_type") == "normal"
        ),
        key=lambda item: _attr(item, "set_number", 0),
    )


def _effective_prescription(exercise: Any) -> dict[str, Any] | None:
    return _attr(exercise, "live_prescription") or _attr(
        exercise, "prescription"
    )


def _prescription_bounds(sets: list[dict[str, Any]]) -> tuple[int | None, int | None, int | None]:
    if not sets:
        return None, None, None
    rep_mins = [int(item["rep_min"]) for item in sets if item.get("rep_min") is not None]
    rep_maxes = [int(item["rep_max"]) for item in sets if item.get("rep_max") is not None]
    rep_max = max(rep_maxes) if len(rep_maxes) == len(sets) else None
    rir = sets[0].get("rir")
    return min(rep_mins) if rep_mins else None, rep_max, int(rir) if rir is not None else None


def _fact_bounds(sets: list[Any]) -> tuple[int | None, int | None, int | None]:
    reps = [int(_attr(item, "reps")) for item in sets if _attr(item, "reps") is not None]
    rir = next(
        (_attr(item, "rir") for item in sets if _attr(item, "rir") is not None),
        None,
    )
    return (
        min(reps) if reps else None,
        max(reps) if reps else None,
        int(rir) if rir is not None else None,
    )


def _set_kinds(
    prescription_sets: list[dict[str, Any]], target_sets: int
) -> list[WorkoutStructureSetKind]:
    result: list[WorkoutStructureSetKind] = []
    for index in range(target_sets):
        kind = (
            prescription_sets[index].get("kind")
            if index < len(prescription_sets)
            else None
        )
        result.append(kind if kind in {"amrap", "backoff"} else "normal")
    return result


def extract_workout_structure(
    workout: Any,
    *,
    persistent_note_ids: Iterable[str] | None = None,
) -> WorkoutStructureDraft:
    note_ids = set(persistent_note_ids or ())
    exercises: list[WorkoutStructureExerciseDraft] = []

    for exercise in sorted(
        _attr(workout, "exercises", []) or [],
        key=lambda item: _attr(item, "order_index", 0),
    ):
        facts = _completed_working_sets(exercise)
        prescription = _effective_prescription(exercise) or {}
        prescription_sets = list(prescription.get("sets") or [])
        raw_target_sets = _attr(exercise, "target_sets")
        target_sets = max(0, int(raw_target_sets) if raw_target_sets is not None else len(facts))
        rep_min, rep_max, target_rir = (
            _prescription_bounds(prescription_sets)
            if prescription_sets
            else _fact_bounds(facts)
        )
        exercise_id = int(_attr(exercise, "id"))
        exercises.append(
            WorkoutStructureExerciseDraft(
                exercise_id=int(_attr(exercise, "exercise_id")),
                order_index=int(_attr(exercise, "order_index", 0)),
                superset_group=_attr(exercise, "superset_group"),
                target_sets=target_sets,
                set_kinds=_set_kinds(prescription_sets, target_sets),
                rep_min=rep_min,
                rep_max=rep_max,
                target_rir=target_rir,
                rest_seconds=_attr(exercise, "rest_seconds"),
                notes=(
                    _normalized_note(_attr(exercise, "notes"))
                    if f"exercise:{exercise_id}" in note_ids
                    else None
                ),
            )
        )

    workout_id = int(_attr(workout, "id"))
    return WorkoutStructureDraft(
        name=_default_name(_attr(workout, "started_at")),
        notes=(
            _normalized_note(_attr(workout, "notes"))
            if f"workout:{workout_id}" in note_ids
            else None
        ),
        exercises=exercises,
    )
