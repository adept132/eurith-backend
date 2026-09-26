"""Оркестратор движка: порядок пяти шагов (спека P0-06 §5.4)."""

import pytest

from api.services.progression import params
from api.services.progression.engine import plan_exercise
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)


def presc(weight, rep_min=8, rep_max=12, sets_count=3) -> Prescription:
    return Prescription(
        scheme=params.SCHEME_DOUBLE,
        sets=tuple(
            SetPrescription(n, weight, rep_min, rep_max, 2, "normal")
            for n in range(1, sets_count + 1)
        ),
        reason_code="progressed",
        reason_text="x",
    )


def session(idx, weight, reps_per_set, *, is_deload=False) -> SessionFact:
    return SessionFact(
        session_id=idx,
        finished_at=None,
        prescription=presc(weight, sets_count=len(reps_per_set)),
        sets=tuple(SetFact(i + 1, weight, r, 2) for i, r in enumerate(reps_per_set)),
        is_deload=is_deload,
    )


def ctx(*sessions, **kw) -> SchemeContext:
    base = dict(
        history=ExerciseHistory(exercise_id=1, sessions=tuple(reversed(sessions))),
        state=ProgressionState(),
        last_outcome=None,
        target_sets=3,
        rep_min=8,
        rep_max=12,
        rep_range_source=params.REP_SOURCE_FALLBACK,
        target_rir=2,
        equipment=("barbell",),
        experience_level="intermediate",
        phase_effort_tier="medium",
        days_since_last_session=3,
    )
    base.update(kw)
    return SchemeContext(**base)


def test_empty_history_returns_no_basis():
    p = plan_exercise(ctx())
    assert p.reason_code == "no_basis"
    assert p.sets == ()


def test_state_is_rebuilt_even_if_caller_passed_an_empty_one():
    # Оркестратор обязан сам восстановить состояние из истории.
    p = plan_exercise(ctx(session(1, 40.0, [10, 9, 7])))
    assert p.sets, "предписание не должно быть пустым при непустой истории"


def test_successful_ceiling_session_advances_the_weight():
    p = plan_exercise(ctx(session(1, 40.0, [12, 12, 12])))
    assert p.top_weight == pytest.approx(42.5)
    assert p.reason_code == "progressed"


def test_single_miss_holds_the_weight():
    p = plan_exercise(ctx(session(1, 40.0, [10, 9, 5])))
    assert p.top_weight == pytest.approx(40.0)
    assert p.reason_code == "goal_met_in_session"


def test_two_misses_in_a_row_reduce_the_weight():
    p = plan_exercise(
        ctx(session(1, 40.0, [7, 7, 7]), session(2, 40.0, [6, 6, 6]))
    )
    assert p.top_weight < 40.0
    assert p.reason_code == "repeated_miss"


def test_later_set_reaching_original_goal_prevents_next_workout_reduction():
    previous = SessionFact(
        session_id=1,
        finished_at=None,
        prescription=presc(40.0, sets_count=2),
        initial_prescription=presc(40.0, sets_count=2),
        sets=(SetFact(1, 40.0, 7, 2), SetFact(2, 40.0, 9, 2)),
    )

    planned = plan_exercise(ctx(previous, target_sets=2))

    assert planned.top_weight == pytest.approx(40.0)
    assert planned.reason_code == "goal_met_in_session"


def test_first_miss_then_adjusted_goal_holds_weight_once():
    original = presc(40.0, sets_count=2)
    adjusted = Prescription(
        scheme=original.scheme,
        sets=(original.sets[0], SetPrescription(2, 37.5, 8, 12, 2, "normal")),
        reason_code=original.reason_code,
        reason_text=original.reason_text,
    )
    previous = SessionFact(
        session_id=1,
        finished_at=None,
        prescription=adjusted,
        initial_prescription=original,
        sets=(SetFact(1, 40.0, 7, 2), SetFact(2, 37.5, 8, 2)),
    )

    planned = plan_exercise(ctx(previous, target_sets=2))

    assert planned.top_weight == pytest.approx(40.0)
    assert planned.reason_code == "hold_after_miss"


def test_two_workouts_missing_original_goal_reduce_even_if_adjusted_sets_succeeded():
    original = presc(40.0, sets_count=2)
    adjusted = Prescription(
        scheme=original.scheme,
        sets=(original.sets[0], SetPrescription(2, 37.5, 8, 12, 2, "normal")),
        reason_code=original.reason_code,
        reason_text=original.reason_text,
    )
    previous = [
        SessionFact(
            session_id=number,
            finished_at=None,
            prescription=adjusted,
            initial_prescription=original,
            sets=(SetFact(1, 40.0, 7, 2), SetFact(2, 37.5, 8, 2)),
        )
        for number in (1, 2)
    ]

    planned = plan_exercise(ctx(*previous, target_sets=2))

    assert planned.top_weight < 40.0
    assert planned.reason_code == "repeated_miss"


def test_override_changes_the_scheme():
    p = plan_exercise(
        ctx(session(1, 100.0, [5, 5, 5]), rep_min=5, rep_max=5),
        override=params.SCHEME_PERCENT_1RM,
    )
    assert p.scheme == params.SCHEME_PERCENT_1RM


def test_reason_code_and_text_are_always_populated():
    for c in [ctx(), ctx(session(1, 40.0, [12, 12, 12])), ctx(session(1, 40.0, [3]))]:
        p = plan_exercise(c)
        assert p.reason_code
        assert p.reason_text


def test_engine_version_is_stamped():
    from api.services.progression.types import ENGINE_VERSION

    p = plan_exercise(ctx(session(1, 40.0, [12, 12, 12])))
    assert p.engine_version == ENGINE_VERSION


def test_provisional_flag_is_propagated():
    p = plan_exercise(ctx(session(1, 40.0, [12, 12, 12])), provisional=True)
    assert p.provisional is True
