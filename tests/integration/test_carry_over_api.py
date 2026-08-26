from __future__ import annotations

import uuid
import asyncio
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from api.services.models import (
    AppUser,
    AppUserProfile,
    Exercise,
    TrainingBlock,
    UserCalendarDay,
    WorkoutCarryOver,
    WorkoutPlan,
    WorkoutPlanExercise,
    WorkoutSession,
    WorkoutSessionExercise,
)


async def _seed_carry_over(db, user_id: int):
    marker = uuid.uuid4().hex[:10]
    profile = await db.scalar(
        select(AppUserProfile).where(AppUserProfile.app_user_id == user_id)
    )
    if profile is None:
        db.add(
            AppUserProfile(
                app_user_id=user_id,
                experience_level="beginner",
                settings={"locations": ["gym"]},
            )
        )

    exercise = Exercise(
        name=f"Carry-over press {marker}",
        category="base",
        main_muscle_group="chest",
        secondary_muscle_groups=[],
        equipment_needed=["barbell"],
        difficulty="beginner",
        source="custom",
        app_user_id=user_id,
    )
    db.add(exercise)
    await db.flush()

    block = TrainingBlock(
        app_user_id=user_id,
        block_index=1000 + int(marker[:4], 16),
        phases=[
            {
                "phase_number": 1,
                "name": "Base",
                "effort_tier": "medium",
                "length_days": 14,
            }
        ],
        microcycle_length=7,
        start_date=date.today(),
        planned_end_date=date.today() + timedelta(days=13),
        status="active",
    )
    db.add(block)
    await db.flush()

    def make_plan(name: str, sets: int) -> WorkoutPlan:
        plan = WorkoutPlan(
            app_user_id=user_id,
            name=f"{name} {marker}",
            day_tag="push",
            micro_tag="medium",
            meso_tag="medium",
        )
        plan.exercises.append(
            WorkoutPlanExercise(
                exercise_id=exercise.id,
                order_index=0,
                target_sets=sets,
            )
        )
        db.add(plan)
        return plan

    source_plan = make_plan("Source", 4)
    future_plan_a = make_plan("Future A", 2)
    future_plan_b = make_plan("Future B", 2)
    next_cycle_plan = make_plan("Next cycle", 1)
    await db.flush()

    source_day = UserCalendarDay(
        app_user_id=user_id,
        target_date=date.today(),
        plan_id=source_plan.id,
        block_id=block.id,
        microcycle_day_number=1,
        day_tag="push",
        micro_tag="medium",
        meso_tag="medium",
        status="completed",
    )
    receiving_days = [
        UserCalendarDay(
            app_user_id=user_id,
            target_date=date.today() + timedelta(days=offset),
            plan_id=plan.id,
            block_id=block.id,
            microcycle_day_number=offset + 1,
            day_tag="push",
            micro_tag="medium",
            meso_tag="medium",
            status="planned",
        )
        for offset, plan in ((1, future_plan_a), (2, future_plan_b))
    ]
    next_cycle_day = UserCalendarDay(
        app_user_id=user_id,
        target_date=date.today() + timedelta(days=7),
        plan_id=next_cycle_plan.id,
        block_id=block.id,
        microcycle_day_number=1,
        day_tag="push",
        micro_tag="medium",
        meso_tag="medium",
        status="planned",
    )
    db.add_all([source_day, *receiving_days, next_cycle_day])
    await db.flush()

    session = WorkoutSession(
        app_user_id=user_id,
        source="plan",
        status="finished",
        plan_id=source_plan.id,
        training_block_id=block.id,
        calendar_day_id=source_day.id,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(session)
    await db.flush()
    source_day.actual_workout_session_id = session.id
    source_exercise = WorkoutSessionExercise(
        workout_session_id=session.id,
        exercise_id=exercise.id,
        order_index=0,
        target_sets=4,
    )
    db.add(source_exercise)
    await db.commit()
    return session, source_exercise, receiving_days, next_cycle_day


def _preview_payload(source_exercise_id: int, key: str | None = None) -> dict:
    return {
        "idempotency_key": key or str(uuid.uuid4()),
        "unfinished_sets": [
            {"source_exercise_id": source_exercise_id, "requested_sets": 4}
        ],
    }


async def test_preview_is_deterministic_idempotent_and_has_no_calendar_side_effects(
    client, db, test_user
):
    session, source_exercise, receiving_days, next_cycle_day = await _seed_carry_over(
        db, test_user.id
    )
    payload = _preview_payload(source_exercise.id)

    first = await client.post(
        f"/workouts/{session.id}/carry-over/preview", json=payload
    )
    second = await client.post(
        f"/workouts/{session.id}/carry-over/preview", json=payload
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    body = first.json()
    assert len(body["preview_token"]) == 64
    item = body["items"][0]
    source_name = await db.scalar(
        select(Exercise.name).where(Exercise.id == source_exercise.exercise_id)
    )
    assert item["source_exercise_name"] == source_name
    assert [placement["target_exercise_name"] for placement in item["placements"]] == [
        source_name,
        source_name,
    ]
    assert [placement["target_day_name"] for placement in item["placements"]] == [
        "push",
        "push",
    ]
    assert [item["delta_sets"] for item in item["placements"]] == [2, 2]
    await db.rollback()
    days = (
        await db.execute(
            select(UserCalendarDay)
            .where(UserCalendarDay.id.in_([day.id for day in receiving_days] + [next_cycle_day.id]))
            .order_by(UserCalendarDay.target_date)
        )
    ).scalars().all()
    assert all(day.volume_adjustments is None for day in days)
    assert await db.scalar(
        select(func.count(WorkoutCarryOver.id)).where(
            WorkoutCarryOver.source_session_id == session.id
        )
    ) == 1


async def test_concurrent_identical_preview_retries_share_one_draft(
    client, db, test_user
):
    session, source_exercise, _, _ = await _seed_carry_over(db, test_user.id)
    payload = _preview_payload(source_exercise.id)

    first, second = await asyncio.gather(
        client.post(f"/workouts/{session.id}/carry-over/preview", json=payload),
        client.post(f"/workouts/{session.id}/carry-over/preview", json=payload),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    await db.rollback()
    assert await db.scalar(
        select(func.count(WorkoutCarryOver.id)).where(
            WorkoutCarryOver.source_session_id == session.id
        )
    ) == 1


async def test_apply_is_explicit_atomic_and_idempotent(client, db, test_user):
    session, source_exercise, receiving_days, next_cycle_day = await _seed_carry_over(
        db, test_user.id
    )
    preview = await client.post(
        f"/workouts/{session.id}/carry-over/preview",
        json=_preview_payload(source_exercise.id),
    )
    assert preview.status_code == 200, preview.text
    preview_item = preview.json()["items"][0]
    exercise = await db.get(Exercise, source_exercise.exercise_id)
    exercise.name = "Renamed after preview"
    for day in receiving_days:
        day.day_tag = "Renamed day after preview"
    await db.commit()
    payload = {"preview_token": preview.json()["preview_token"]}

    first = await client.post(
        f"/workouts/{session.id}/carry-over/apply", json=payload
    )
    replay = await client.post(
        f"/workouts/{session.id}/carry-over/apply", json=payload
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["items"][0] == preview_item
    assert replay.json() == first.json()
    await db.rollback()
    days = (
        await db.execute(
            select(UserCalendarDay)
            .where(UserCalendarDay.id.in_([day.id for day in receiving_days] + [next_cycle_day.id]))
            .order_by(UserCalendarDay.target_date)
        )
    ).scalars().all()
    assert [day.volume_adjustments[0]["delta_sets"] for day in days[:2]] == [2, 2]
    assert all(day.volume_adjustments is None for day in days[2:])
    carry_over_id = first.json()["items"][0]["carry_over_id"]
    for day in days[:2]:
        adjustment = day.volume_adjustments[0]
        assert adjustment["carry_over_id"] == carry_over_id
        assert adjustment["provenance"]["source_session_id"] == session.id
    row = await db.get(WorkoutCarryOver, carry_over_id, populate_existing=True)
    assert row.status == "applied"
    assert row.applied_at is not None


async def test_concurrent_apply_retries_do_not_escape_as_server_errors(
    client, db, test_user
):
    session, source_exercise, _, _ = await _seed_carry_over(db, test_user.id)
    preview = await client.post(
        f"/workouts/{session.id}/carry-over/preview",
        json=_preview_payload(source_exercise.id),
    )
    assert preview.status_code == 200, preview.text
    payload = {"preview_token": preview.json()["preview_token"]}

    first, second = await asyncio.gather(
        client.post(f"/workouts/{session.id}/carry-over/apply", json=payload),
        client.post(f"/workouts/{session.id}/carry-over/apply", json=payload),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()


async def test_preview_retry_and_dismiss_share_source_then_draft_lock_order(
    client, db, test_user
):
    session, source_exercise, _, _ = await _seed_carry_over(db, test_user.id)
    preview_payload = _preview_payload(source_exercise.id)
    preview = await client.post(
        f"/workouts/{session.id}/carry-over/preview", json=preview_payload
    )
    assert preview.status_code == 200, preview.text
    decision_payload = {"preview_token": preview.json()["preview_token"]}

    retried, dismissed = await asyncio.gather(
        client.post(
            f"/workouts/{session.id}/carry-over/preview", json=preview_payload
        ),
        client.post(
            f"/workouts/{session.id}/carry-over/dismiss", json=decision_payload
        ),
    )

    assert dismissed.status_code == 200, dismissed.text
    assert retried.status_code in {200, 409}, retried.text
    await db.rollback()
    statuses = set(
        (
            await db.execute(
                select(WorkoutCarryOver.status).where(
                    WorkoutCarryOver.source_session_id == session.id
                )
            )
        ).scalars().all()
    )
    assert statuses == {"dismissed"}


async def test_stale_apply_returns_fresh_preview_without_carry_over_mutation(
    client, db, test_user
):
    session, source_exercise, receiving_days, _ = await _seed_carry_over(
        db, test_user.id
    )
    preview = await client.post(
        f"/workouts/{session.id}/carry-over/preview",
        json=_preview_payload(source_exercise.id),
    )
    stale_token = preview.json()["preview_token"]
    preview_item = preview.json()["items"][0]
    exercise = await db.get(Exercise, source_exercise.exercise_id)
    exercise.name = "Renamed after preview"
    receiving_days[0].day_tag = "Renamed day after preview"
    receiving_days[1].day_tag = "Renamed day after preview"
    source_plan = await db.get(WorkoutPlan, session.plan_id)
    source_plan.revision += 1
    await db.commit()

    applied = await client.post(
        f"/workouts/{session.id}/carry-over/apply",
        json={"preview_token": stale_token},
    )

    assert applied.status_code == 409, applied.text
    assert applied.json()["detail"]["error"] == "stale_preview"
    assert applied.json()["detail"]["preview"]["preview_token"] != stale_token
    assert applied.json()["detail"]["preview"]["items"][0] == preview_item
    await db.rollback()
    days = (
        await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.id.in_([day.id for day in receiving_days])
            )
        )
    ).scalars().all()
    assert all(day.volume_adjustments is None for day in days)


async def test_dismiss_has_no_calendar_side_effects_and_blocks_apply(
    client, db, test_user
):
    session, source_exercise, receiving_days, _ = await _seed_carry_over(
        db, test_user.id
    )
    preview = await client.post(
        f"/workouts/{session.id}/carry-over/preview",
        json=_preview_payload(source_exercise.id),
    )
    token = preview.json()["preview_token"]
    preview_item = preview.json()["items"][0]
    exercise = await db.get(Exercise, source_exercise.exercise_id)
    exercise.name = "Renamed after preview"
    for day in receiving_days:
        day.day_tag = "Renamed day after preview"
    await db.commit()

    dismissed = await client.post(
        f"/workouts/{session.id}/carry-over/dismiss",
        json={"preview_token": token},
    )
    applied = await client.post(
        f"/workouts/{session.id}/carry-over/apply",
        json={"preview_token": token},
    )

    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["status"] == "dismissed"
    assert dismissed.json()["items"][0] == preview_item
    assert applied.status_code == 409
    await db.rollback()
    days = (
        await db.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.id.in_([day.id for day in receiving_days])
            )
        )
    ).scalars().all()
    assert all(day.volume_adjustments is None for day in days)


async def test_preview_hides_a_foreign_workout(client, db, test_user):
    marker = uuid.uuid4().hex[:10]
    foreign = AppUser(
        firebase_uid=f"foreign-carry-{marker}",
        email=f"foreign-carry-{marker}@example.com",
    )
    db.add(foreign)
    await db.flush()
    workout = WorkoutSession(
        app_user_id=foreign.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc),
    )
    db.add(workout)
    await db.commit()

    response = await client.post(
        f"/workouts/{workout.id}/carry-over/preview",
        json=_preview_payload(999999),
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "source_not_found"
    await db.execute(delete(AppUser).where(AppUser.id == foreign.id))
    await db.commit()
