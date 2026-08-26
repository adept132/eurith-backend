from __future__ import annotations

import importlib
import os
from datetime import date
from datetime import datetime, timezone

import pytest

from tests.fixtures.scheduling import carry_over_candidate


def _service():
    return importlib.import_module("api.services.carry_over")


def _distribute(requested_sets: int, *candidate_values):
    service = _service()
    candidates = [service.CarryOverCandidate(**item) for item in candidate_values]
    return service.distribute_carry_over(requested_sets, candidates)


def _placement_counts(result):
    return [
        (item.calendar_day_id, item.exercise_id, item.delta_sets, item.match_kind)
        for item in result.placements
    ]


def test_effective_plan_sets_clamps_oversized_negative_adjustment_for_safety_budgets():
    service = _service()

    effective = service._effective_plan_sets(
        [(101, 2), (202, 4)],
        [
            {"exercise_id": 101, "delta_sets": -99},
            {"exercise_id": 202, "delta_sets": 1},
        ],
    )

    # The same effective counts feed both the per-session cap and the
    # whole-microcycle MRV budget.  The canonical adjustment contract never
    # removes an exercise: an oversized cut bottoms out at one set.
    assert effective == [1, 5]


def test_effective_plan_sets_preserves_duplicate_exercise_rows():
    service = _service()

    effective = service._effective_plan_sets(
        [(101, 2), (101, 3)],
        [{"exercise_id": 101, "delta_sets": 1}],
    )

    assert effective == [3, 4]


def test_serialization_failure_detection_supports_asyncpg_wrapping():
    service = _service()

    class DriverError(Exception):
        sqlstate = "40001"

    class WrappedError(Exception):
        orig = DriverError()

    assert service._is_serialization_failure(WrappedError()) is True
    assert service._is_serialization_failure(RuntimeError("other")) is False


def test_spreads_four_sets_two_plus_two_across_exact_days():
    result = _distribute(
        4,
        carry_over_candidate(calendar_day_id=10, session_cap_remaining=2),
        carry_over_candidate(
            calendar_day_id=20,
            target_date=date(2026, 8, 26),
            session_cap_remaining=2,
        ),
    )

    assert _placement_counts(result) == [
        (10, 10, 2, "exact"),
        (20, 10, 2, "exact"),
    ]
    assert result.discarded_reasons == []


def test_exhausts_exact_exercise_days_before_compatible_muscle_day():
    result = _distribute(
        4,
        carry_over_candidate(calendar_day_id=10, session_cap_remaining=2),
        carry_over_candidate(
            calendar_day_id=20,
            target_date=date(2026, 8, 26),
            session_cap_remaining=2,
        ),
        carry_over_candidate(
            calendar_day_id=15,
            target_date=date(2026, 8, 25),
            exercise_id=11,
            match_kind="compatible",
            session_cap_remaining=4,
        ),
    )

    assert _placement_counts(result) == [
        (10, 10, 2, "exact"),
        (20, 10, 2, "exact"),
    ]


def test_compatible_muscle_day_is_used_when_exact_capacity_is_exhausted():
    result = _distribute(
        3,
        carry_over_candidate(calendar_day_id=10, session_cap_remaining=1),
        carry_over_candidate(
            calendar_day_id=20,
            target_date=date(2026, 8, 26),
            exercise_id=11,
            match_kind="compatible",
            session_cap_remaining=3,
        ),
    )

    assert _placement_counts(result) == [
        (10, 10, 1, "exact"),
        (20, 11, 2, "compatible"),
    ]


def test_distribution_keeps_exact_and_compatible_display_context_snapshots():
    """A later rename must not change the labels selected for this preview."""
    service = _service()
    requests = [
        service.CarryOverSourceRequest(
            source_exercise_id=101,
            source_exercise_name="Source press",
            exercise_id=10,
            muscle="chest",
            requested_sets=2,
            candidates=[
                service.CarryOverCandidate(
                    **carry_over_candidate(
                        calendar_day_id=10,
                        target_exercise_name="Exact press",
                        target_day_name="Upper A",
                        session_cap_remaining=1,
                    )
                ),
                service.CarryOverCandidate(
                    **carry_over_candidate(
                        calendar_day_id=20,
                        target_date=date(2026, 8, 26),
                        exercise_id=11,
                        match_kind="compatible",
                        target_exercise_name="Compatible fly",
                        target_day_name="Upper B",
                        session_cap_remaining=1,
                    )
                ),
            ],
        )
    ]

    result = service.distribute_carry_over_sources(requests)[0]

    assert result.source_exercise_name == "Source press"
    assert [
        (item.target_exercise_name, item.target_day_name, item.match_kind)
        for item in result.distribution.placements
    ] == [
        ("Exact press", "Upper A", "exact"),
        ("Compatible fly", "Upper B", "compatible"),
    ]


def test_decision_payload_excludes_target_display_names():
    service = _service()
    original = _distribute(
        1,
        carry_over_candidate(
            target_exercise_name="Original press",
            target_day_name="Original upper",
        ),
    )
    renamed = _distribute(
        1,
        carry_over_candidate(
            target_exercise_name="Renamed press",
            target_day_name="Renamed upper",
        ),
    )

    assert service._placement_decision_payload(original) == (
        service._placement_decision_payload(renamed)
    ) == [
        {
            "calendar_day_id": 1,
            "target_date": "2026-08-24",
            "exercise_id": 10,
            "delta_sets": 1,
            "match_kind": "exact",
        }
    ]


def test_stale_snapshot_labels_follow_target_identity_across_match_kind_change():
    service = _service()
    original = _distribute(
        1,
        carry_over_candidate(
            target_exercise_name="Original press",
            target_day_name="Original upper",
            match_kind="compatible",
        ),
    )
    refreshed = _distribute(
        1,
        carry_over_candidate(
            target_exercise_name="Renamed press",
            target_day_name="Renamed upper",
            match_kind="exact",
        ),
    )

    persisted = service._placement_payload(original)
    restored = service._placement_payload(refreshed, persisted)

    assert restored[0]["match_kind"] == "exact"
    assert restored[0]["target_exercise_name"] == "Original press"
    assert restored[0]["target_day_name"] == "Original upper"


def test_session_cap_discards_every_set_that_cannot_fit():
    result = _distribute(
        3,
        carry_over_candidate(session_cap_remaining=1),
    )

    assert _placement_counts(result) == [(1, 10, 1, "exact")]
    assert result.discarded_reasons == ["session_cap", "session_cap"]


def test_mrv_is_a_shared_microcycle_budget_across_candidate_days():
    result = _distribute(
        4,
        carry_over_candidate(calendar_day_id=10, muscle_mrv_remaining=2),
        carry_over_candidate(
            calendar_day_id=20,
            target_date=date(2026, 8, 26),
            muscle_mrv_remaining=2,
        ),
    )

    assert sum(item.delta_sets for item in result.placements) == 2
    assert result.discarded_reasons == ["mrv", "mrv"]


@pytest.mark.parametrize(
    ("blocked", "reason"),
    [
        ({"is_deload": True}, "deload"),
        ({"readiness_level": "limit"}, "readiness"),
        ({"has_pain": True}, "pain"),
        ({"equipment_available": False}, "equipment"),
    ],
)
def test_hard_safety_blocks_zero_candidate_capacity(blocked, reason):
    result = _distribute(2, carry_over_candidate(**blocked))

    assert result.placements == []
    assert result.discarded_reasons == [reason, reason]


def test_never_places_past_current_microcycle_end():
    result = _distribute(
        2,
        carry_over_candidate(within_microcycle=False),
    )

    assert result.placements == []
    assert result.discarded_reasons == ["microcycle_end", "microcycle_end"]


def test_order_is_deterministic_by_date_then_calendar_id():
    result = _distribute(
        3,
        carry_over_candidate(
            calendar_day_id=30,
            target_date=date(2026, 8, 25),
            session_cap_remaining=1,
        ),
        carry_over_candidate(
            calendar_day_id=20,
            target_date=date(2026, 8, 24),
            session_cap_remaining=1,
        ),
        carry_over_candidate(
            calendar_day_id=10,
            target_date=date(2026, 8, 24),
            session_cap_remaining=1,
        ),
    )

    assert [item.calendar_day_id for item in result.placements] == [10, 20, 30]


def test_session_and_mrv_budgets_are_shared_across_source_exercises():
    service = _service()
    day_one = carry_over_candidate(
        calendar_day_id=10,
        session_cap_remaining=1,
        muscle_mrv_remaining=3,
    )
    day_two = carry_over_candidate(
        calendar_day_id=20,
        target_date=date(2026, 8, 26),
        session_cap_remaining=4,
        muscle_mrv_remaining=3,
    )
    requests = [
        service.CarryOverSourceRequest(
            source_exercise_id=101,
            source_exercise_name="First press",
            exercise_id=10,
            muscle="chest",
            requested_sets=2,
            candidates=[service.CarryOverCandidate(**day_one), service.CarryOverCandidate(**day_two)],
        ),
        service.CarryOverSourceRequest(
            source_exercise_id=102,
            source_exercise_name="Second press",
            exercise_id=11,
            muscle="chest",
            requested_sets=2,
            candidates=[
                service.CarryOverCandidate(**{**day_one, "exercise_id": 11}),
                service.CarryOverCandidate(**{**day_two, "exercise_id": 11}),
            ],
        ),
    ]

    results = service.distribute_carry_over_sources(requests)
    placed_on_day_one = sum(
        placement.delta_sets
        for item in results
        for placement in item.distribution.placements
        if placement.calendar_day_id == 10
    )

    assert placed_on_day_one == 1
    assert sum(item.distribution.placed_sets for item in results) == 3
    assert sum(item.distribution.discarded_sets for item in results) == 1


@pytest.mark.asyncio
async def test_preview_loads_only_exact_days_from_the_source_microcycle():
    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url or test_database_url.rstrip("/").endswith("/fitpilot_bot"):
        pytest.skip("requires a dedicated TEST_DATABASE_URL")
    os.environ["DATABASE_URL"] = test_database_url

    from app.database import SessionLocal, init_db
    from api.services.models import (
        AppUser,
        AppUserProfile,
        Exercise,
        TrainingBlock,
        UserCalendarDay,
        UserObservation,
        WorkoutPlan,
        WorkoutPlanExercise,
        WorkoutSession,
        WorkoutSessionExercise,
    )

    await init_db()
    async with SessionLocal() as db:
        user = AppUser(
            firebase_uid="carry-over-distribution-user",
            email="carry-over-distribution@example.com",
        )
        db.add(user)
        await db.flush()
        db.add(
            AppUserProfile(
                app_user_id=user.id,
                experience_level="beginner",
                settings={"locations": ["gym"]},
            )
        )
        exact = Exercise(
            name="Carry-over exact press",
            category="base",
            main_muscle_group="chest",
            secondary_muscle_groups=[],
            equipment_needed=["barbell"],
            difficulty="beginner",
            source="custom",
            app_user_id=user.id,
        )
        compatible = Exercise(
            name="Carry-over compatible fly",
            category="isolation",
            main_muscle_group="chest",
            secondary_muscle_groups=[],
            equipment_needed=["dumbbell"],
            difficulty="beginner",
            source="custom",
            app_user_id=user.id,
        )
        db.add_all([exact, compatible])
        await db.flush()

        block = TrainingBlock(
            app_user_id=user.id,
            block_index=1,
            phases=[
                {
                    "phase_number": 1,
                    "name": "Base",
                    "effort_tier": "medium",
                    "length_days": 14,
                }
            ],
            microcycle_length=7,
            start_date=date(2026, 8, 20),
            planned_end_date=date(2026, 9, 4),
            status="active",
        )
        db.add(block)
        await db.flush()

        def plan(name: str, exercise: Exercise, sets: int) -> WorkoutPlan:
            item = WorkoutPlan(
                app_user_id=user.id,
                name=name,
                day_tag="push",
                micro_tag="medium",
                meso_tag="medium",
            )
            item.exercises.append(
                WorkoutPlanExercise(
                    exercise_id=exercise.id,
                    order_index=0,
                    target_sets=sets,
                )
            )
            db.add(item)
            return item

        source_plan = plan("Source", exact, 4)
        exact_plan_a = plan("Exact A", exact, 2)
        exact_plan_b = plan("Exact B", exact, 2)
        compatible_plan = plan("Compatible", compatible, 2)
        next_cycle_plan = plan("Next cycle", exact, 1)
        await db.flush()

        source_day = UserCalendarDay(
            app_user_id=user.id,
            target_date=date(2026, 8, 22),
            plan_id=source_plan.id,
            block_id=block.id,
            microcycle_day_number=3,
            day_tag="push",
            micro_tag="medium",
            meso_tag="medium",
        )
        future_days = [
            UserCalendarDay(
                app_user_id=user.id,
                target_date=target_date,
                plan_id=plan_row.id,
                block_id=block.id,
                microcycle_day_number=day_number,
                day_tag="push",
                micro_tag="medium",
                meso_tag="medium",
            )
            for target_date, plan_row, day_number in (
                (date(2026, 8, 23), exact_plan_a, 4),
                (date(2026, 8, 24), exact_plan_b, 5),
                (date(2026, 8, 25), compatible_plan, 6),
                (date(2026, 8, 29), next_cycle_plan, 1),
            )
        ]
        db.add_all([source_day, *future_days])
        await db.flush()

        source_session = WorkoutSession(
            app_user_id=user.id,
            source="plan",
            status="finished",
            plan_id=source_plan.id,
            training_block_id=block.id,
            calendar_day_id=source_day.id,
            finished_at=datetime(2026, 8, 22, 10, tzinfo=timezone.utc),
        )
        db.add(source_session)
        await db.flush()
        source_exercise = WorkoutSessionExercise(
            workout_session_id=source_session.id,
            exercise_id=exact.id,
            order_index=0,
            target_sets=4,
        )
        db.add(source_exercise)
        await db.flush()

        service = _service()
        preview = await service.preview_carry_over(
            db,
            user.id,
            source_session.id,
            {source_exercise.id: 4},
            datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        )

        assert preview.microcycle_end == date(2026, 8, 28)
        assert len(preview.items) == 1
        assert _placement_counts(preview.items[0].distribution) == [
            (future_days[0].id, exact.id, 2, "exact"),
            (future_days[1].id, exact.id, 2, "exact"),
        ]
        assert all(
            item.calendar_day_id != future_days[-1].id
            for item in preview.items[0].distribution.placements
        )

        # Earlier current-cycle volume is part of the hard MRV budget even
        # though only post-source days are placement candidates.
        earlier_plan = plan("Earlier", exact, 5)
        await db.flush()
        earlier_day = UserCalendarDay(
            app_user_id=user.id,
            target_date=date(2026, 8, 20),
            plan_id=earlier_plan.id,
            block_id=block.id,
            microcycle_day_number=1,
            day_tag="push",
            micro_tag="medium",
            meso_tag="medium",
        )
        db.add(earlier_day)
        await db.flush()
        mrv_preview = await service.preview_carry_over(
            db,
            user.id,
            source_session.id,
            {source_exercise.id: 4},
            datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        )
        assert mrv_preview.items[0].distribution.placements == []
        assert mrv_preview.items[0].distribution.discarded_reasons == ["mrv"] * 4
        await db.delete(earlier_day)
        await db.delete(earlier_plan)
        await db.flush()

        db.add(
            UserObservation(
                app_user_id=user.id,
                kind="pain",
                value=2,
                subject="shoulder",
                source="checkin",
                observed_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
            )
        )
        await db.flush()
        pain_preview = await service.preview_carry_over(
            db,
            user.id,
            source_session.id,
            {source_exercise.id: 4},
            datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        )
        assert pain_preview.items[0].distribution.placements == []
        assert pain_preview.items[0].distribution.discarded_reasons == ["pain"] * 4

        await db.rollback()
