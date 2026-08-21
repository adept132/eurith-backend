from __future__ import annotations

from datetime import datetime
from typing import Protocol, TypeVar


class WorkoutLifecycle(Protocol):
    status: str
    started_at: datetime
    finished_at: datetime | None
    paused_at: datetime | None
    paused_duration_seconds: int


WorkoutT = TypeVar("WorkoutT", bound=WorkoutLifecycle)


class InvalidWorkoutTransition(ValueError):
    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition workout from {current} to {target}")


def _closed_pause_seconds(workout: WorkoutLifecycle, now: datetime) -> int:
    if workout.paused_at is None:
        return 0
    return max(0, int((now - workout.paused_at).total_seconds()))


def pause(workout: WorkoutT, now: datetime) -> WorkoutT:
    if workout.status == "finished":
        raise InvalidWorkoutTransition("finished", "paused")
    if workout.status == "active":
        workout.status = "paused"
        workout.paused_at = now
    return workout


def resume(workout: WorkoutT, now: datetime) -> WorkoutT:
    if workout.status == "finished":
        raise InvalidWorkoutTransition("finished", "active")
    if workout.status == "paused":
        workout.paused_duration_seconds += _closed_pause_seconds(workout, now)
        workout.paused_at = None
        workout.status = "active"
    return workout


def finish(workout: WorkoutT, now: datetime) -> WorkoutT:
    if workout.status == "finished":
        return workout
    if workout.status == "paused":
        workout.paused_duration_seconds += _closed_pause_seconds(workout, now)
        workout.paused_at = None
    workout.status = "finished"
    workout.finished_at = now
    return workout


def apply_snapshot(
    workout: WorkoutT,
    *,
    status: str,
    paused_at: datetime | None,
    paused_duration_seconds: int,
    finished_at: datetime | None,
    now: datetime,
) -> WorkoutT:
    """Apply client-authoritative lifecycle fields without reopening a finish."""
    if workout.status == "finished":
        return workout

    workout.paused_duration_seconds = max(0, paused_duration_seconds)

    if status == "paused":
        if workout.status == "active":
            pause(workout, paused_at or now)
        else:
            workout.paused_at = paused_at or workout.paused_at or now
        workout.finished_at = None
        return workout

    if status == "active":
        workout.paused_at = None
        resume(workout, now)
        workout.finished_at = None
        return workout

    if status == "finished":
        if paused_at is not None:
            workout.status = "paused"
            workout.paused_at = paused_at
        return finish(workout, finished_at or now)

    raise InvalidWorkoutTransition(workout.status, status)


def effective_duration_seconds(workout: WorkoutLifecycle, now: datetime) -> int:
    end = workout.finished_at or now
    elapsed = max(0, int((end - workout.started_at).total_seconds()))
    paused = max(0, workout.paused_duration_seconds or 0)
    if workout.paused_at is not None:
        paused += max(0, int((end - workout.paused_at).total_seconds()))
    return max(0, elapsed - paused)
