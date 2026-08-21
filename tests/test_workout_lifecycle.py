import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.routers.workouts as workouts_router

from api.services.workout_lifecycle import (
    InvalidWorkoutTransition,
    apply_snapshot,
    effective_duration_seconds,
    finish,
    pause,
    resume,
)


T0 = datetime(2026, 8, 22, 10, tzinfo=timezone.utc)


@pytest.fixture
def workout():
    return SimpleNamespace(
        status="active",
        started_at=T0 - timedelta(minutes=10),
        finished_at=None,
        paused_at=None,
        paused_duration_seconds=0,
    )


def test_pause_resume_accumulates_once(workout):
    pause(workout, T0)
    pause(workout, T0 + timedelta(seconds=5))
    resume(workout, T0 + timedelta(seconds=30))
    resume(workout, T0 + timedelta(seconds=35))

    assert workout.paused_duration_seconds == 30
    assert workout.paused_at is None
    assert workout.status == "active"


def test_finish_from_pause_closes_interval(workout):
    pause(workout, T0)
    finish(workout, T0 + timedelta(seconds=45))

    assert workout.status == "finished"
    assert workout.finished_at == T0 + timedelta(seconds=45)
    assert workout.paused_duration_seconds == 45
    assert workout.paused_at is None


def test_finish_is_idempotent_and_preserves_first_timestamp(workout):
    finish(workout, T0)
    finish(workout, T0 + timedelta(minutes=5))

    assert workout.finished_at == T0


def test_finished_workout_cannot_pause_or_resume(workout):
    finish(workout, T0)

    with pytest.raises(InvalidWorkoutTransition):
        pause(workout, T0 + timedelta(seconds=1))
    with pytest.raises(InvalidWorkoutTransition):
        resume(workout, T0 + timedelta(seconds=1))


def test_effective_duration_excludes_closed_and_open_pauses(workout):
    workout.started_at = T0
    workout.paused_duration_seconds = 20
    workout.status = "paused"
    workout.paused_at = T0 + timedelta(seconds=70)

    assert effective_duration_seconds(workout, T0 + timedelta(seconds=100)) == 50


def test_clock_rollback_never_subtracts_negative_pause(workout):
    workout.started_at = T0
    pause(workout, T0 + timedelta(seconds=20))
    resume(workout, T0 + timedelta(seconds=10))

    assert workout.paused_duration_seconds == 0
    assert effective_duration_seconds(workout, T0 + timedelta(seconds=10)) == 10


def test_sync_snapshot_never_rolls_finished_back(workout):
    finish(workout, T0)

    apply_snapshot(
        workout,
        status="paused",
        paused_at=T0 + timedelta(minutes=1),
        paused_duration_seconds=60,
        finished_at=None,
        now=T0 + timedelta(minutes=1),
    )

    assert workout.status == "finished"
    assert workout.finished_at == T0


def test_sync_finished_snapshot_closes_open_pause(workout):
    apply_snapshot(
        workout,
        status="finished",
        paused_at=T0,
        paused_duration_seconds=5,
        finished_at=T0 + timedelta(seconds=30),
        now=T0 + timedelta(minutes=1),
    )

    assert workout.status == "finished"
    assert workout.paused_at is None
    assert workout.paused_duration_seconds == 35
    assert workout.finished_at == T0 + timedelta(seconds=30)


class FakeResult:
    def __init__(self, workout):
        self.workout = workout

    def scalar_one_or_none(self):
        return self.workout


class FakeSession:
    def __init__(self, workout):
        self.workout = workout
        self.committed = False
        self.refreshed = False

    async def execute(self, _stmt):
        return FakeResult(self.workout)

    async def commit(self):
        self.committed = True

    async def refresh(self, workout):
        assert workout is self.workout
        self.refreshed = True


def test_pause_endpoint_mutates_owned_workout(workout):
    session = FakeSession(workout)

    result = asyncio.run(
        workouts_router.pause_workout(
            7,
            db=session,
            current_app_user=SimpleNamespace(id=3),
        )
    )

    assert result.status == "paused"
    assert result.paused_at is not None
    assert session.committed is True
    assert session.refreshed is True


def test_resume_endpoint_returns_not_found_without_owned_workout():
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            workouts_router.resume_workout(
                7,
                db=FakeSession(None),
                current_app_user=SimpleNamespace(id=3),
            )
        )

    assert error.value.status_code == 404


def test_pause_endpoint_rejects_finished_workout(workout):
    workout.status = "finished"

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            workouts_router.pause_workout(
                7,
                db=FakeSession(workout),
                current_app_user=SimpleNamespace(id=3),
            )
        )

    assert error.value.status_code == 409
