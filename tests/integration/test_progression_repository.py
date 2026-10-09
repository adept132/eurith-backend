"""Репозиторий движка прогрессии: загрузка истории, write-once, проекция."""

from datetime import datetime, timezone

import pytest

from api.services.models import Exercise, WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet
from api.services.load_context import EffectiveLoadContext
from api.services.progression import repository
from api.services.progression.types import Prescription, SetPrescription


def sample_prescription(weight=42.5) -> Prescription:
    return Prescription(
        scheme="double",
        sets=(
            SetPrescription(1, weight, 8, 12, 2, "normal"),
            SetPrescription(2, weight, 8, 12, 2, "normal"),
        ),
        reason_code="progressed",
        reason_text="Прошлая цель выполнена — двигаемся вперёд.",
        basis={"anchor_weight": 40.0},
    )


@pytest.mark.asyncio
async def test_persist_writes_prescription_and_projection(db_session, session_exercise):
    repository.persist_prescription(session_exercise, sample_prescription())
    await db_session.flush()

    assert session_exercise.prescription["scheme"] == "double"
    # Проекция первого рабочего подхода — для fatigue/, csv_format.py и
    # старых клиентов.
    assert float(session_exercise.recommended_weight) == pytest.approx(42.5)
    assert session_exercise.recommended_rep_min == 8
    assert session_exercise.recommended_rep_max == 12
    assert session_exercise.recommended_rir == 2


@pytest.mark.asyncio
async def test_projection_carries_the_trimmed_set_count(db_session, session_exercise):
    """P0-07: подрезка объёма должна быть ВИДНА пользователю.

    apply_volume_trim убирает подходы из Prescription.sets, но интерфейс
    показывает плоское target_sets. Без этой проекции причина «сократили
    число подходов» появлялась, а сама цифра не менялась.
    """
    session_exercise.target_sets = 4
    await db_session.flush()

    # Предписание на два подхода — как после трима с уровнем limit.
    repository.persist_prescription(session_exercise, sample_prescription())
    await db_session.flush()

    assert len(session_exercise.prescription["sets"]) == 2
    assert session_exercise.target_sets == 2


@pytest.mark.asyncio
async def test_persist_is_write_once(db_session, session_exercise):
    repository.persist_prescription(session_exercise, sample_prescription(42.5))
    await db_session.flush()
    repository.persist_prescription(session_exercise, sample_prescription(99.0))
    await db_session.flush()

    # Пользователь уже видел первое предписание — подменять его нельзя.
    assert float(session_exercise.recommended_weight) == pytest.approx(42.5)
    assert session_exercise.prescription["sets"][0]["weight_kg"] == pytest.approx(42.5)


@pytest.mark.asyncio
async def test_empty_prescription_does_not_overwrite_projection(
    db_session, session_exercise
):
    empty = Prescription(
        scheme="double", sets=(), reason_code="no_basis", reason_text="x"
    )
    repository.persist_prescription(session_exercise, empty)
    await db_session.flush()
    assert session_exercise.recommended_weight is None


@pytest.mark.asyncio
async def test_refresh_state_is_idempotent(db_session, app_user, exercise):
    await repository.refresh_state(db_session, app_user.id, exercise.id, None)
    await db_session.flush()
    await repository.refresh_state(db_session, app_user.id, exercise.id, None)
    await db_session.flush()

    rows = await repository.debug_all_states(db_session, app_user.id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_load_history_returns_newest_first(db_session, app_user, exercise):
    history = await repository.load_history(db_session, app_user.id, exercise.id)
    ids = [s.session_id for s in history.sessions]
    assert ids == sorted(ids, reverse=True)


@pytest.mark.asyncio
async def test_machine_history_filters_by_setup_and_preserves_each_shown_target(db_session, app_user, exercise):
    from uuid import uuid4

    gym_id, stack_id, plate_id = uuid4(), uuid4(), uuid4()
    workout = WorkoutSession(app_user_id=app_user.id, source="free", status="finished", finished_at=datetime.now(timezone.utc))
    db_session.add(workout)
    await db_session.flush()
    se = WorkoutSessionExercise(workout_session_id=workout.id, exercise_id=exercise.id, order_index=0)
    db_session.add(se)
    await db_session.flush()
    for number, mode, setup_id, weight, target in (
        (1, "stack", stack_id, 60, 60),
        (2, "plate_loaded", plate_id, 30, 30),
        (3, None, None, 100, None),
    ):
        db_session.add(WorkoutSessionSet(
            workout_session_exercise_id=se.id, set_number=number, set_type="normal",
            weight=weight, reps=8, is_completed=True, load_mode=mode,
            gym_profile_id=gym_id if mode else None, setup_id=setup_id,
            load_snapshot={"weight_basis": "displayed" if mode == "stack" else "plates_only"} if mode else None,
            shown_target_snapshot={"set_number": number, "weight_kg": target,
                                   "rep_min": 8, "rep_max": 12, "rir": 2, "kind": "normal"} if target else None,
        ))
    db_session.add_all([
        WorkoutSessionSet(
            workout_session_exercise_id=se.id, set_number=4, set_type="normal",
            weight=25, reps=8, is_completed=True, load_mode="plate_loaded",
            gym_profile_id=gym_id, setup_id=None,
            load_snapshot={"weight_basis": "plates_only"},
        ),
        WorkoutSessionSet(
            workout_session_exercise_id=se.id, set_number=5, set_type="normal",
            weight=35, reps=8, is_completed=True, load_mode="plate_loaded",
            gym_profile_id=gym_id, setup_id=plate_id,
            load_snapshot={"weight_basis": "including_start_weight"},
        ),
    ])
    await db_session.flush()

    def context(mode, setup, basis):
        return EffectiveLoadContext(gym_id, "Gym", setup, mode, 5, "kg", 1, basis, None, None)

    stack = await repository.load_history(db_session, app_user.id, exercise.id, load_context=context("stack", stack_id, "displayed"))
    plate = await repository.load_history(db_session, app_user.id, exercise.id, load_context=context("plate_loaded", plate_id, "plates_only"))
    temporary = await repository.load_history(db_session, app_user.id, exercise.id, load_context=context("plate_loaded", None, "plates_only"))
    general = await repository.load_history(db_session, app_user.id, exercise.id)
    assert [s.weight_kg for s in stack.sessions[0].sets] == [60]
    assert [s.weight_kg for s in plate.sessions[0].sets] == [30]
    assert [s.weight_kg for s in temporary.sessions[0].sets] == [25]
    assert [s.weight_kg for s in general.sessions[0].sets] == [60, 30, 100, 25, 35]
    assert stack.sessions[0].sets[0].shown_target.weight_kg == 60
    assert plate.sessions[0].sets[0].shown_target.weight_kg == 30


# --- Блокер 1 финального ревью P0-06: принадлежность предписания упражнению ---
#
# full_replace (api/routers/exercises.py) меняет WorkoutSessionExercise.exercise_id
# на другое упражнение, но write-once prescription остаётся посчитанным для
# СТАРОГО. Без защиты упражнение без единой собственной тренировки получает
# чужую цель: evaluate() сравнивает факт с чужим предписанием, а
# _has_prescription_history() решает, что бутстрап уже пройден.


@pytest.mark.asyncio
async def test_persist_prescription_stamps_owner_exercise_id(db_session, session_exercise):
    """persist_prescription штампует exercise_id строки в basis — единственная
    подпись принадлежности, которая переживёт full_replace."""
    repository.persist_prescription(session_exercise, sample_prescription())
    await db_session.flush()

    assert (
        session_exercise.prescription["basis"]["exercise_id"]
        == session_exercise.exercise_id
    )


@pytest.mark.asyncio
async def test_load_history_rejects_prescription_from_other_exercise(
    db_session, app_user, exercise
):
    """Смоделированный full_replace: строка с prescription, посчитанным для
    other_ex, "переезжает" на exercise (exercise_id меняется, prescription —
    нет, как это реально делает write-once). load_history для exercise
    обязана деградировать эту сессию до "нет предписания", как для легитимно
    пустого se.prescription is None — тот же путь, что и для неразбираемого
    JSON (см. комментарий в load_history)."""
    other_ex = Exercise(
        name="Другое упражнение (P0-06, блокер 1)",
        category="base",
        main_muscle_group="back",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=app_user.id,
    )
    db_session.add(other_ex)
    await db_session.flush()

    workout = WorkoutSession(
        app_user_id=app_user.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(workout)
    await db_session.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=other_ex.id,
        order_index=0,
    )
    db_session.add(se)
    await db_session.flush()

    # Предписание считается и штампуется, пока строка ещё принадлежит other_ex.
    repository.persist_prescription(se, sample_prescription())
    await db_session.flush()

    # full_replace: exercise_id строки меняется на exercise, prescription
    # (write-once) остаётся от other_ex.
    se.exercise_id = exercise.id
    await db_session.flush()

    history = await repository.load_history(db_session, app_user.id, exercise.id)
    assert len(history.sessions) == 1
    assert history.sessions[0].prescription is None, (
        "load_history доверилась предписанию другого упражнения — "
        "защита принадлежности (P0-06, блокер 1) не сработала"
    )


@pytest.mark.asyncio
async def test_load_history_trusts_prescription_without_owner_stamp(
    db_session, app_user, exercise
):
    """Обратная совместимость: у ВСЕХ предписаний, сохранённых до этого
    фикса, метки basis["exercise_id"] нет вовсе. Отсутствие метки — это НЕ
    признак чужого предписания, а "принадлежность неизвестна" -> доверяем,
    иначе одним махом обнулится вся накопленная история предписаний."""
    workout = WorkoutSession(
        app_user_id=app_user.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(workout)
    await db_session.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=exercise.id,
        order_index=0,
    )
    db_session.add(se)
    await db_session.flush()

    # Легаси-запись: prescription сохранён МИМО persist_prescription (как
    # было бы у строк, осевших в БД до фикса) — в basis нет exercise_id.
    se.prescription = sample_prescription().to_dict()
    await db_session.flush()

    history = await repository.load_history(db_session, app_user.id, exercise.id)
    assert len(history.sessions) == 1
    assert history.sessions[0].prescription is not None
    assert history.sessions[0].prescription.scheme == "double"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_state", ["complete", "unfinished", "anomalous", "zero_reps", "missing_weight"])
async def test_working_goal_mapping_ignores_warmup_positions(db_session, app_user, exercise, first_state):
    from api.services.progression.state import evaluate
    workout = WorkoutSession(app_user_id=app_user.id, source="free", status="finished", finished_at=datetime.now(timezone.utc))
    db_session.add(workout)
    await db_session.flush()
    p = Prescription(scheme="double", sets=tuple(SetPrescription(i, w, 10, 12, 2, "normal")
        for i, w in enumerate((80, 70, 60), 1)), reason_code="test", reason_text="")
    se = WorkoutSessionExercise(workout_session_id=workout.id, exercise_id=exercise.id, order_index=0, prescription=p.to_dict())
    db_session.add(se)
    await db_session.flush()
    for number in range(1, 4):
        db_session.add(WorkoutSessionSet(workout_session_exercise_id=se.id, set_number=number,
            set_type="warmup", weight=200, reps=20, is_completed=True))
    roots = []
    for ordinal, weight in enumerate((80, 70, 60), 1):
        row = WorkoutSessionSet(workout_session_exercise_id=se.id, set_number=(4, 6, 7)[ordinal - 1],
            set_type="normal", weight=weight, reps=10, effort_level="medium", is_completed=True)
        if ordinal == 1:
            row.is_completed = first_state != "unfinished"
            row.is_anomalous = first_state == "anomalous"
            row.reps = 0 if first_state == "zero_reps" else 10
            row.weight = None if first_state == "missing_weight" else weight
        roots.append(row)
        db_session.add(row)
    await db_session.flush()
    db_session.add(WorkoutSessionSet(workout_session_exercise_id=se.id, set_number=5,
        set_type="normal", parent_set_id=roots[0].id, weight=200, reps=20, is_completed=True))
    await db_session.flush()
    history = await repository.load_history(db_session, app_user.id, exercise.id)
    facts = [s for s in history.sessions[0].sets if s.set_type == "normal"]
    expected_ordinals = [2, 3] if first_state == "unfinished" else [1, 2, 3]
    assert [(s.set_number, getattr(s, "working_ordinal", None)) for s in facts] == [((4, 6, 7)[i - 1], i) for i in expected_ordinals]
    out = evaluate(p, history.sessions[0].sets, 2.5)
    assert out.hit_sets == (3 if first_state == "complete" else 2)
    assert out.status == "hit"


@pytest.mark.asyncio
@pytest.mark.parametrize("shown", [True, False])
async def test_variant_history_ignores_warmup_only_sessions_and_warmup_targets(db_session, app_user, exercise, shown):
    from uuid import uuid4
    from datetime import timedelta
    gym, setup = uuid4(), uuid4()
    context = EffectiveLoadContext(gym, "Gym", setup, "stack", 5, "kg", 1, "displayed", None, None)
    p = Prescription(scheme="double", sets=(SetPrescription(1, 80, 10, 12, 2, "normal"),
        SetPrescription(2, 50, 8, 12, 2, "normal")), reason_code="test", reason_text="")
    ids = []
    for index in range(2):
        workout = WorkoutSession(app_user_id=app_user.id, source="free", status="finished",
            finished_at=datetime.now(timezone.utc) + timedelta(minutes=index))
        db_session.add(workout)
        await db_session.flush()
        se = WorkoutSessionExercise(workout_session_id=workout.id, exercise_id=exercise.id, order_index=0, prescription=p.to_dict())
        db_session.add(se)
        await db_session.flush()
        ids.append(workout.id)
        common = dict(workout_session_exercise_id=se.id, is_completed=True, load_mode="stack",
            gym_profile_id=gym, setup_id=setup, load_snapshot={"weight_basis": "displayed"})
        db_session.add(WorkoutSessionSet(**common, set_number=1, set_type="warmup", weight=200, reps=20))
        if index == 0:
            # Another variant consumes ordinal 1 even when variant filtering removes it.
            db_session.add(WorkoutSessionSet(workout_session_exercise_id=se.id, set_number=2,
                set_type="normal", weight=80, reps=10, is_completed=True))
            target = SetPrescription(99, 50, 8, 12, 2, "normal").to_dict()
            db_session.add(WorkoutSessionSet(**common, set_number=3, set_type="normal", weight=50, reps=10,
                shown_target_snapshot=target if shown else None))
        await db_session.flush()
    history = await repository.load_history(db_session, app_user.id, exercise.id, limit=1, load_context=context)
    assert [s.session_id for s in history.sessions] == ids[:1]
    session = history.sessions[0]
    working = [s for s in session.sets if s.set_type == "normal"]
    assert working[0].set_number == 3
    assert working[0].working_ordinal == 2
    if shown:
        assert session.prescription.sets == (working[0].shown_target,)
        assert session.initial_prescription == session.prescription
    else:
        assert session.prescription == p
        from api.services.progression.state import evaluate
        assert evaluate(session.prescription, session.sets, 2.5).hit_sets == 1


@pytest.mark.asyncio
async def test_warmup_excluded_from_working_records(db_session, app_user, exercise):
    from copy import deepcopy
    from sqlalchemy import select
    from api.services.models import UserExerciseProgressionState
    from api.services.progression.records_repository import rebuild_records
    workout = WorkoutSession(app_user_id=app_user.id, source="free", status="finished", finished_at=datetime.now(timezone.utc))
    db_session.add(workout)
    await db_session.flush()
    se = WorkoutSessionExercise(workout_session_id=workout.id, exercise_id=exercise.id, order_index=0)
    db_session.add(se)
    await db_session.flush()
    for i in range(4, 7):
        db_session.add(WorkoutSessionSet(workout_session_exercise_id=se.id, set_number=i,
            set_type="normal", weight=80, reps=10, is_completed=True, is_anomalous=False))
    await db_session.flush()
    await rebuild_records(db_session, app_user.id, [exercise.id])
    state = (await db_session.execute(select(UserExerciseProgressionState).where(
        UserExerciseProgressionState.app_user_id == app_user.id, UserExerciseProgressionState.exercise_id == exercise.id))).scalar_one()
    baseline = deepcopy(state.records)
    for i in range(1, 4):
        db_session.add(WorkoutSessionSet(workout_session_exercise_id=se.id, set_number=i,
            set_type="warmup", weight=200, reps=20, is_completed=True, is_anomalous=False))
    await db_session.flush()
    await rebuild_records(db_session, app_user.id, [exercise.id])
    assert state.records == baseline
    assert state.records["set_volume"]["value"] == 800
