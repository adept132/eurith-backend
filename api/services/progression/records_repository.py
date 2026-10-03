"""Пересчёт личных рекордов по сырым подходам (P1-14).

Отдельно от progression.repository намеренно: та функция строит
состояние движка из ExerciseHistory — окна последних HISTORY_LIMIT=12
сессий. Для прогрессии окна достаточно, для рекордов — нет.
"""

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    UserExerciseProgressionState,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.progression.records import SetInput, fold_records


def load_variant_key(mode: str | None, gym_id, setup_id, snapshot: dict | None) -> str | None:
    """Stable grouping for a machine's recorded weight basis.

    Unknown legacy sets deliberately have no machine variant.
    """
    if mode not in ("stack", "plate_loaded") or not isinstance(snapshot, dict):
        return None
    basis = snapshot.get("weight_basis")
    if basis not in ("displayed", "plates_only", "including_start_weight"):
        return None
    if setup_id is not None:
        return f"setup:{setup_id}|basis:{basis}"
    return f"gym:{gym_id if gym_id is not None else 'none'}|mode:{mode}|basis:{basis}"


async def rebuild_records(
    session: AsyncSession,
    app_user_id: int,
    exercise_ids: Iterable[int],
) -> None:
    """Пересчитать колонку records для указанных упражнений.

    Фильтр допуска дословно совпадает с /progress/achievements: иначе
    живая плашка и хронология достижений разошлись бы.
    """
    ids = [int(x) for x in exercise_ids]
    if not ids:
        return

    rows = (await session.execute(
        select(
            WorkoutSessionExercise.exercise_id,
            WorkoutSessionSet.weight,
            WorkoutSessionSet.reps,
            WorkoutSession.finished_at,
            WorkoutSession.id,
            WorkoutSessionSet.load_mode,
            WorkoutSessionSet.gym_profile_id,
            WorkoutSessionSet.setup_id,
            WorkoutSessionSet.load_snapshot,
        )
        .select_from(WorkoutSessionSet)
        .join(WorkoutSessionExercise)
        .join(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "finished",
            WorkoutSession.finished_at.is_not(None),
            WorkoutSessionExercise.exercise_id.in_(ids),
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.is_anomalous.is_(False),
            WorkoutSessionSet.set_type == "normal",
            WorkoutSessionSet.weight.is_not(None),
            WorkoutSessionSet.reps.is_not(None),
            WorkoutSessionSet.weight > 0,
            WorkoutSessionSet.reps > 0,
        )
    )).all()

    by_exercise: dict[int, list[SetInput]] = {ex_id: [] for ex_id in ids}
    by_variant: dict[int, dict[str, list[SetInput]]] = {ex_id: {} for ex_id in ids}
    for exercise_id, weight, reps, finished_at, workout_id, mode, gym_id, setup_id, snapshot in rows:
        entry = SetInput(
            weight=float(weight),
            reps=int(reps),
            at=finished_at,
            workout_id=workout_id,
        )
        by_exercise[exercise_id].append(entry)
        key = load_variant_key(mode, gym_id, setup_id, snapshot)
        if key is not None:
            by_variant[exercise_id].setdefault(key, []).append(entry)

    existing = {
        row.exercise_id: row
        for row in (await session.execute(
            select(UserExerciseProgressionState).where(
                UserExerciseProgressionState.app_user_id == app_user_id,
                UserExerciseProgressionState.exercise_id.in_(ids),
            )
        )).scalars().all()
    }

    for exercise_id, sets in by_exercise.items():
        row = existing.get(exercise_id)
        if row is None:
            row = UserExerciseProgressionState(
                app_user_id=app_user_id, exercise_id=exercise_id,
            )
            session.add(row)
        records = fold_records(sets)
        records["variants"] = {
            key: _isoformat_dates(fold_records(items))
            for key, items in by_variant[exercise_id].items()
        }
        records["variants_complete"] = True
        # datetime не сериализуется в JSONB как есть.
        row.records = _isoformat_dates(records)


def _isoformat_dates(records: dict) -> dict:
    def conv(entry: dict | None) -> dict | None:
        if entry is None:
            return None
        out = dict(entry)
        at = out.get("at")
        if at is not None and not isinstance(at, str):
            out["at"] = at.isoformat()
        return out

    output = {
        "weight_at_reps": {k: conv(v) for k, v in records["weight_at_reps"].items()},
        "band": {k: conv(v) for k, v in records["band"].items()},
        "set_volume": conv(records["set_volume"]),
    }
    if "variants" in records:
        output["variants"] = records["variants"]
        output["variants_complete"] = records.get("variants_complete", False)
    return output
