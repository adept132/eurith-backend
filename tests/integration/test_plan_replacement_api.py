from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUser,
    Exercise,
    SplitBlueprint,
    UserCalendarDay,
    UserSplit,
    WorkoutPlan,
    WorkoutPlanExercise,
    WorkoutRoutine,
    WorkoutRoutineExercise,
    WorkoutSession,
)


def _exercise(marker: str, name: str, muscle: str) -> Exercise:
    return Exercise(
        name=f"{name}-{marker}",
        category="strength",
        fatigue_tier=2,
        main_muscle_group=muscle,
        secondary_muscle_groups=[],
        equipment_needed=[],
        difficulty="intermediate",
        source="default",
    )


async def _seed_preview(db, user_id: int, *, include_locked_days: bool = True):
    marker = uuid.uuid4().hex[:10]
    old_exercise = _exercise(marker, "Old press", "chest")
    new_exercise = _exercise(marker, "New press", "triceps")
    db.add_all([old_exercise, new_exercise])
    await db.flush()

    plan = WorkoutPlan(
        app_user_id=user_id,
        name="Push",
        day_tag="push",
        micro_tag="adaptive",
        meso_tag="adaptive",
        revision=0,
        is_archived=False,
    )
    db.add(plan)
    await db.flush()
    db.add(
        WorkoutPlanExercise(
            plan_id=plan.id,
            exercise_id=old_exercise.id,
            order_index=0,
            target_sets=3,
            override_reps="6-8",
            override_rir=3,
        )
    )

    routine = WorkoutRoutine(
        app_user_id=user_id,
        client_uuid=str(uuid.uuid4()),
        name="Replacement",
        sort_order=0,
        revision=0,
        exercises=[
            WorkoutRoutineExercise(
                exercise_id=new_exercise.id,
                order_index=0,
                target_sets=4,
                set_kinds=["normal"] * 4,
                rep_min=8,
                rep_max=10,
                target_rir=2,
                rest_seconds=120,
            )
        ],
    )
    db.add(routine)
    await db.flush()

    today = date.today()
    eligible_dates = [today + timedelta(days=1), today + timedelta(days=8)]
    eligible = [
        UserCalendarDay(
            app_user_id=user_id,
            target_date=target_date,
            plan_id=plan.id,
            day_tag="push",
            status="planned",
            is_rest_day=False,
        )
        for target_date in eligible_dates
    ]
    completed = UserCalendarDay(
        app_user_id=user_id,
        target_date=today + timedelta(days=2),
        plan_id=plan.id,
        day_tag="push",
        status="completed",
        actual_workout_session_id=777,
        is_rest_day=False,
    )
    active_day = UserCalendarDay(
        app_user_id=user_id,
        target_date=today + timedelta(days=3),
        plan_id=plan.id,
        day_tag="push",
        status="planned",
        is_rest_day=False,
    )
    db.add_all(eligible)
    if include_locked_days:
        db.add_all([completed, active_day])
        await db.flush()
        db.add(
            WorkoutSession(
                app_user_id=user_id,
                source="plan",
                status="active",
                plan_id=plan.id,
                calendar_day_id=active_day.id,
                started_at=datetime.now(timezone.utc),
            )
        )
    await db.commit()
    return plan, routine, eligible_dates, old_exercise.name, new_exercise.name


async def test_routine_preview_is_deterministic_and_excludes_completed_and_active_days(
    client, db, test_user
):
    plan, routine, eligible_dates, old_name, new_name = await _seed_preview(
        db, test_user.id
    )

    first = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )
    second = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    body = first.json()
    assert body["affected_dates"] == [item.isoformat() for item in eligible_dates]
    assert body["old_total_sets"] == 3
    assert body["new_total_sets"] == 4
    assert [item["exercise_name"] for item in body["removed"]] == [old_name]
    assert [item["exercise_name"] for item in body["added"]] == [new_name]
    assert body["preview_token"] == second.json()["preview_token"]


async def test_preview_hides_a_plan_owned_by_another_user(client, db, test_user):
    _, routine, _, _, _ = await _seed_preview(db, test_user.id)
    marker = uuid.uuid4().hex[:10]
    other = AppUser(
        firebase_uid=f"other-{marker}",
        email=f"other-{marker}@example.com",
        display_name="Other",
    )
    db.add(other)
    await db.flush()
    foreign_plan = WorkoutPlan(
        app_user_id=other.id,
        name="Foreign",
        day_tag="push",
        micro_tag="adaptive",
        meso_tag="adaptive",
        revision=0,
        is_archived=False,
    )
    db.add(foreign_plan)
    await db.commit()

    response = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": foreign_plan.id},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "target_plan_not_found"
    await db.execute(delete(AppUser).where(AppUser.id == other.id))
    await db.commit()


async def test_workout_preview_rejects_an_empty_structure(client, db, test_user):
    plan, _, _, _, _ = await _seed_preview(db, test_user.id)
    workout = WorkoutSession(
        app_user_id=test_user.id,
        source="free",
        status="finished",
        started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        finished_at=datetime.now(timezone.utc),
    )
    db.add(workout)
    await db.commit()

    response = await client.post(
        f"/workouts/{workout.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "empty_replacement_structure"


async def test_apply_clones_metadata_and_rebinds_only_unfinished_inactive_days(
    client, db, test_user
):
    plan, routine, eligible_dates, _, _ = await _seed_preview(db, test_user.id)
    preview = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )
    assert preview.status_code == 200

    applied = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/apply",
        json={
            "target_plan_id": plan.id,
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": str(uuid.uuid4()),
        },
    )

    assert applied.status_code == 200
    assert applied.json()["status"] == "applied"
    assert applied.json()["affected_dates"] == [
        value.isoformat() for value in eligible_dates
    ]
    new_plan_id = applied.json()["new_plan_id"]
    await db.rollback()
    new_plan = (
        await db.execute(
            select(WorkoutPlan)
            .where(WorkoutPlan.id == new_plan_id)
            .options(selectinload(WorkoutPlan.exercises))
        )
    ).scalar_one()
    old_plan = await db.get(WorkoutPlan, plan.id, populate_existing=True)
    days = (
        await db.execute(
            select(UserCalendarDay)
            .where(UserCalendarDay.app_user_id == test_user.id)
            .order_by(UserCalendarDay.target_date)
        )
    ).scalars().all()
    active_session = (
        await db.execute(
            select(WorkoutSession).where(
                WorkoutSession.app_user_id == test_user.id,
                WorkoutSession.status == "active",
            )
        )
    ).scalar_one()

    assert new_plan.supersedes_plan_id == plan.id
    assert new_plan.name == plan.name
    assert (new_plan.day_tag, new_plan.micro_tag, new_plan.meso_tag) == (
        plan.day_tag,
        plan.micro_tag,
        plan.meso_tag,
    )
    assert new_plan.revision == plan.revision + 1
    assert [(item.exercise_id, item.target_sets, item.override_reps, item.override_rir) for item in new_plan.exercises] == [
        (routine.exercises[0].exercise_id, 4, "8-10", 2)
    ]
    assert [day.plan_id for day in days if day.target_date in eligible_dates] == [
        new_plan_id,
        new_plan_id,
    ]
    assert all(
        day.plan_id == plan.id
        for day in days
        if day.target_date not in eligible_dates
    )
    assert active_session.plan_id == plan.id
    assert old_plan.is_archived is False


async def test_apply_returns_refreshed_preview_for_a_stale_token(client, db, test_user):
    plan, routine, _, _, _ = await _seed_preview(db, test_user.id)
    preview = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )
    stale_token = preview.json()["preview_token"]
    plan.revision += 1
    await db.commit()
    before = await db.scalar(select(func.count(WorkoutPlan.id)))

    applied = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/apply",
        json={
            "target_plan_id": plan.id,
            "preview_token": stale_token,
            "idempotency_key": str(uuid.uuid4()),
        },
    )

    assert applied.status_code == 409
    assert applied.json()["detail"]["error"] == "stale_preview"
    assert applied.json()["detail"]["preview"]["preview_token"] != stale_token
    await db.rollback()
    assert await db.scalar(select(func.count(WorkoutPlan.id))) == before


async def test_validator_failure_is_atomic(client, db, test_user):
    plan, routine, eligible_dates, _, _ = await _seed_preview(db, test_user.id)
    await db.execute(
        update(WorkoutRoutineExercise)
        .where(WorkoutRoutineExercise.routine_id == routine.id)
        .values(target_sets=7, set_kinds=["normal"] * 7)
    )
    await db.commit()
    preview = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )
    before = await db.scalar(select(func.count(WorkoutPlan.id)))
    old_plan_id = plan.id

    applied = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/apply",
        json={
            "target_plan_id": plan.id,
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": str(uuid.uuid4()),
        },
    )

    assert applied.status_code == 400
    await db.rollback()
    assert await db.scalar(select(func.count(WorkoutPlan.id))) == before
    plan_ids = (
        await db.execute(
            select(UserCalendarDay.plan_id).where(
                UserCalendarDay.app_user_id == test_user.id,
                UserCalendarDay.target_date.in_(eligible_dates),
            )
        )
    ).scalars().all()
    assert plan_ids == [old_plan_id, old_plan_id]


async def test_apply_archives_old_plan_when_no_future_reference_remains(
    client, db, test_user
):
    plan, routine, _, _, _ = await _seed_preview(
        db, test_user.id, include_locked_days=False
    )
    preview = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )

    applied = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/apply",
        json={
            "target_plan_id": plan.id,
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": str(uuid.uuid4()),
        },
    )

    assert applied.status_code == 200
    listed = await client.get("/plans/")
    assert listed.status_code == 200
    listed_ids = {item["id"] for item in listed.json()}
    assert plan.id not in listed_ids
    assert applied.json()["new_plan_id"] in listed_ids
    await db.rollback()
    old_plan = await db.get(WorkoutPlan, plan.id, populate_existing=True)
    assert old_plan.is_archived is True


async def test_apply_rebinds_legacy_split_selection_and_replay_is_idempotent(
    client, db, test_user
):
    plan, routine, eligible_dates, _, _ = await _seed_preview(
        db, test_user.id, include_locked_days=True
    )
    blueprint = SplitBlueprint(
        name="Review split",
        author_id=test_user.id,
        length_days=1,
        is_system=False,
    )
    db.add(blueprint)
    await db.flush()
    split = UserSplit(
        app_user_id=test_user.id,
        blueprint_id=blueprint.id,
        selected_plans={"1": plan.id, "blackout_weekdays": [6]},
        current_day=1,
        is_active=True,
    )
    db.add(split)
    await db.commit()
    preview = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )
    key = str(uuid.uuid4())
    payload = {
        "target_plan_id": plan.id,
        "preview_token": preview.json()["preview_token"],
        "idempotency_key": key,
    }

    first = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/apply", json=payload
    )
    replay = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/apply", json=payload
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    await db.rollback()
    saved_split = await db.get(UserSplit, split.id, populate_existing=True)
    assert saved_split.selected_plans == {
        "1": first.json()["new_plan_id"],
        "blackout_weekdays": [6],
    }
    successors = await db.scalar(
        select(func.count(WorkoutPlan.id)).where(
            WorkoutPlan.supersedes_plan_id == plan.id
        )
    )
    assert successors == 1
    assert first.json()["affected_dates"] == [
        item.isoformat() for item in eligible_dates
    ]


async def test_idempotency_key_cannot_be_reused_for_a_different_request(
    client, db, test_user
):
    plan, routine, _, _, _ = await _seed_preview(
        db, test_user.id, include_locked_days=False
    )
    preview = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )
    key = str(uuid.uuid4())
    first = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/apply",
        json={
            "target_plan_id": plan.id,
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": key,
        },
    )
    conflict = await client.post(
        f"/workout-routines/{routine.id + 999}/plan-replacement/apply",
        json={
            "target_plan_id": plan.id,
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": key,
        },
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "idempotency_conflict"


async def test_archived_plan_cannot_be_selected_mutated_or_deleted(
    client, db, test_user
):
    plan, routine, _, _, _ = await _seed_preview(
        db, test_user.id, include_locked_days=False
    )
    blueprint = SplitBlueprint(
        name="Immutable split",
        author_id=test_user.id,
        length_days=1,
        is_system=False,
    )
    db.add(blueprint)
    await db.flush()
    db.add(
        UserSplit(
            app_user_id=test_user.id,
            blueprint_id=blueprint.id,
            selected_plans={},
            current_day=1,
            is_active=True,
        )
    )
    await db.commit()
    preview = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/preview",
        json={"target_plan_id": plan.id},
    )
    applied = await client.post(
        f"/workout-routines/{routine.id}/plan-replacement/apply",
        json={
            "target_plan_id": plan.id,
            "preview_token": preview.json()["preview_token"],
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert applied.status_code == 200

    selected = await client.patch(
        "/workout-center/context/plan", json={"plan_id": plan.id}
    )
    mutated = await client.put(
        f"/plans/{plan.id}",
        json={
            "name": "Retroactive edit",
            "day_tag": "push",
            "micro_tag": "adaptive",
            "meso_tag": "adaptive",
            "exercises": [],
        },
    )
    deleted = await client.delete(f"/plans/{plan.id}")

    assert selected.status_code == 404
    assert mutated.status_code == 409
    assert mutated.json()["detail"]["error"] == "immutable_plan_history"
    assert deleted.status_code == 409
    assert deleted.json()["detail"]["error"] == "immutable_plan_history"
