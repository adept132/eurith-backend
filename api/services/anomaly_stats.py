"""Срез истории по упражнению для относительных правил anomaly_guard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.anomaly_guard import ExerciseStats
from api.services.models import (
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)

# Окно наблюдения [КОНФИГ].
HISTORY_DAYS = 90
_UNSET = object()


def anomaly_inputs(row):
    return deepcopy((row.weight, row.reps, row.set_type, row.is_completed,
                     row.analytics_load_snapshot, row.anomaly_review))


async def load_exercise_stats(
    db: AsyncSession, app_user_id: int, exercise_id: int
) -> ExerciseStats:
    """Считает историю по рабочим завершённым подходам за 90 дней.

    Уже помеченные аномалии исключаются: иначе одна ошибка расширила бы
    коридор для следующих.
    """
    since = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)

    base = (
        select(WorkoutSessionSet.weight, WorkoutSessionSet.reps, WorkoutSession.id)
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .join(
            WorkoutSession,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSessionExercise.exercise_id == exercise_id,
            WorkoutSession.started_at >= since,
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.is_anomalous.is_(False),
            WorkoutSessionSet.set_type == "normal",
            WorkoutSessionSet.weight.isnot(None),
            WorkoutSessionSet.reps.isnot(None),
        )
    )

    rows = (await db.execute(base)).all()
    if not rows:
        return ExerciseStats(sessions=0, best_e1rm=None, median_weight_kg=None)

    weights = sorted(float(r[0]) for r in rows)
    session_ids = {r[2] for r in rows}
    best_e1rm = max(float(r[0]) * int(r[1]) ** 0.1 for r in rows)

    mid = len(weights) // 2
    median = (
        weights[mid]
        if len(weights) % 2 == 1
        else (weights[mid - 1] + weights[mid]) / 2
    )

    return ExerciseStats(
        sessions=len(session_ids),
        best_e1rm=best_e1rm,
        median_weight_kg=median,
    )


def normalized_fact(workout, exercise, row):
    from api.services.analytics.load import normalize_set
    from api.services.analytics.types import RawSetFact
    return normalize_set(RawSetFact(
        workout.id, exercise.exercise_id, exercise.id, row.id or 0,
        row.set_number or 1, exercise.order_index or 0, workout.started_at,
        workout.finished_at, str(workout.gym_profile_id) if workout.gym_profile_id else None,
        None, str(row.setup_id) if row.setup_id else None, row.set_type or 'normal',
        bool(row.is_completed), bool(row.is_anomalous), row.weight, row.reps,
        row.effort_level, row.analytics_load_snapshot, workout.analytics_snapshot,
        (workout.analytics_snapshot or {}).get('is_deload')))


async def review_set(db, workout, exercise, row, *, confirmed=None, previous_review=_UNSET):
    """Derived state only: caller owns dirty marking and the transaction.

    Unknown legacy exclusions cannot be cleared by guessing why they were set.
    Confirmations retain their timestamp on exact replay.
    """
    from api.services.analytics.config import analytics_enabled
    from api.services.anomaly_guard import check_analytics_set, check_set, resolve_is_anomalous
    old = row.anomaly_review if previous_review is _UNSET else previous_review
    was_anomalous = row.is_anomalous
    if not analytics_enabled() and row.analytics_load_snapshot is None and old is None:
        if not was_anomalous:
            verdict = check_set(row.weight, row.reps, row.set_type, await load_exercise_stats(
                db, workout.app_user_id, exercise.exercise_id))
            row.is_anomalous = resolve_is_anomalous(verdict, confirmed)
        return
    fact = normalized_fact(workout, exercise, row)
    records = (await db.execute(select(WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet)
        .select_from(WorkoutSessionSet)
        .join(WorkoutSessionExercise, WorkoutSessionExercise.id == WorkoutSessionSet.workout_session_exercise_id)
        .join(WorkoutSession, WorkoutSession.id == WorkoutSessionExercise.workout_session_id)
        .where(WorkoutSession.app_user_id == workout.app_user_id,
               WorkoutSessionExercise.exercise_id == exercise.exercise_id,
               WorkoutSession.id != workout.id, WorkoutSession.started_at < workout.started_at,
               WorkoutSession.started_at >= workout.started_at - timedelta(days=HISTORY_DAYS),
               WorkoutSession.status == 'finished'))).all()
    review = check_analytics_set(fact, [normalized_fact(w, e, s) for w, e, s in records])
    # A very large recorded number is reviewable even if its load basis is unknown.
    if row.weight is not None and row.weight > 600 and 'unusual_weight' not in review.reason_codes:
        review.reason_codes.append('unusual_weight')
    if was_anomalous and (not old or not old.get('reason_codes')):
        review.reason_codes.append('legacy_unknown')
    elif old and 'legacy_unknown' in old.get('reason_codes', []):
        review.reason_codes.append('legacy_unknown')
    # Foreign/non-derived history annotations are durable metadata.
    derived = {'unusual_weight', 'e1rm_jump', 'weight_median_jump', 'legacy_unknown'}
    supplied = row.anomaly_review or {}
    for metadata in (old or {}, supplied):
        review.reason_codes.extend(code for code in metadata.get('reason_codes', [])
                                   if code not in derived and code not in review.reason_codes)
    review.confirmed = bool(supplied.get('confirmed')) if confirmed is None else confirmed
    if review.confirmed:
        review.confirmed_at = supplied.get('confirmed_at') or (old or {}).get('confirmed_at') or datetime.now(timezone.utc)
    from api.schemas.analytics_context import AnomalyReview
    review = AnomalyReview.model_validate(review.model_dump(mode='python', warnings=False))
    row.anomaly_review = review.model_dump(mode='json')
    row.is_anomalous = (bool(was_anomalous and old and any(code not in derived for code in old.get('reason_codes', []))) or
                        'legacy_unknown' in review.reason_codes or
                        bool(review.reason_codes) and not review.confirmed)


async def review_workout(db, workout):
    """Re-evaluate sets after a session mass/context change, without lazy loads."""
    records = (await db.execute(select(WorkoutSessionExercise, WorkoutSessionSet)
        .join(WorkoutSessionSet, WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id)
        .where(WorkoutSessionExercise.workout_session_id == workout.id))).all()
    for exercise, row in records:
        await review_set(db, workout, exercise, row)
