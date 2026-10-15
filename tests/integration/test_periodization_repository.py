"""Создание блока из активных настроек и снимок состояния прогрессии."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    Exercise,
    Mesocycle,
    MesocyclePhase,
    TrainingBlock,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.periodization import params
from api.services.periodization import phases as phase_ops
from api.services.periodization import repository as periodization_repository
from api.services.periodization.repository import (
    block_state,
    build_state_snapshot,
    ensure_active_block,
    get_active_block,
)

TODAY = date(2026, 8, 3)


async def _seed_periodization(db, user_id: int) -> None:
    meso = Mesocycle(
        author_id=user_id,
        name="Тест",
        code=f"t_{uuid.uuid4().hex[:8]}",
        phases_in_cycle=3,
    )
    db.add(meso)
    await db.flush()
    for number, tier in enumerate(["easy", "medium", "deload"], start=1):
        db.add(
            MesocyclePhase(
                mesocycle_id=meso.id, phase_number=number, name=tier, effort_tier=tier
            )
        )
    db.add(
        AppUserMesocycle(
            app_user_id=user_id,
            mesocycle_id=meso.id,
            is_active=True,
            microcycle_length=7,
            current_phase=1,
        )
    )
    db.add(
        AppUserMicrocycle(
            app_user_id=user_id,
            name="Тестовый микроцикл",
            length_days=7,
            days_mapping={"1": {"type": "hard", "tag": "push"}},
            is_active=True,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_ensure_creates_first_block_from_active_settings(db, test_user: AppUser):
    await _seed_periodization(db, test_user.id)

    block = await ensure_active_block(db, test_user.id, TODAY)

    assert block is not None
    assert block.block_index == 1
    assert block.start_date == TODAY, "первый блок начинается сегодня, прошлое не реконструируем"
    assert block.microcycle_length == 7
    assert [p["effort_tier"] for p in block.phases] == ["easy", "medium", "deload"]
    assert block.planned_end_date == date(2026, 8, 23)  # 21 день, включая первый
    assert block.status == "active"


@pytest.mark.asyncio
async def test_ensure_is_idempotent(db, test_user: AppUser):
    await _seed_periodization(db, test_user.id)

    first = await ensure_active_block(db, test_user.id, TODAY)
    second = await ensure_active_block(db, test_user.id, date(2026, 8, 10))

    assert first.id == second.id
    count = len(
        (
            await db.execute(
                select(TrainingBlock).where(TrainingBlock.app_user_id == test_user.id)
            )
        ).scalars().all()
    )
    assert count == 1


@pytest.mark.asyncio
async def test_no_periodization_means_no_block(db, test_user: AppUser):
    """Пустая координата честнее выдуманной."""
    assert await ensure_active_block(db, test_user.id, TODAY) is None
    assert await get_active_block(db, test_user.id) is None


@pytest.mark.asyncio
async def test_block_state_reads_the_snapshot_back(db, test_user: AppUser):
    await _seed_periodization(db, test_user.id)
    block = await ensure_active_block(db, test_user.id, TODAY)

    state = block_state(block)

    assert state.block_index == 1
    assert [p.effort_tier for p in state.phases] == ["easy", "medium", "deload"]
    assert state.start_date == TODAY


# --- Ревью Задачи 6: гонка в ensure_active_block -----------------------------


@pytest.mark.asyncio
async def test_ensure_recovers_when_block_appears_concurrently(db, test_user: AppUser):
    """A separate connection wins creation; a real duplicate INSERT loses locally."""
    from app.database import SessionLocal
    await _seed_periodization(db, test_user.id)
    real_create = periodization_repository.create_block

    async def racing_create(session, app_user_id, start_date, **kwargs):
        async with SessionLocal() as competitor:
            await real_create(competitor, app_user_id, start_date, **kwargs)
            await competitor.commit()
        return await real_create(session, app_user_id, start_date, **kwargs)

    with patch('api.services.periodization.repository.create_block', side_effect=racing_create):
        block = await ensure_active_block(db, test_user.id, TODAY)
    assert block is not None and block.block_index == 1
    blocks = list((await db.execute(select(TrainingBlock).where(
        TrainingBlock.app_user_id == test_user.id))).scalars())
    assert [b.id for b in blocks] == [block.id]


@pytest.mark.asyncio
async def test_ensure_recovers_when_rollover_races_concurrently(db, test_user: AppUser):
    """The competing rollover commits before this connection tries the same index."""
    from app.database import SessionLocal
    await _seed_periodization(db, test_user.id)
    old = await ensure_active_block(db, test_user.id, TODAY - timedelta(days=22))
    old_id, old_index = old.id, old.block_index
    real_close = periodization_repository.close_and_advance

    async def racing_close(session, app_user_id, block, **kwargs):
        async with SessionLocal() as competitor:
            other = await competitor.get(TrainingBlock, old_id)
            await real_close(competitor, app_user_id, other, **kwargs)
            await competitor.commit()
        return await real_close(session, app_user_id, block, **kwargs)

    with patch('api.services.periodization.repository.close_and_advance', side_effect=racing_close):
        block = await ensure_active_block(db, test_user.id, TODAY)
    assert block is not None and block.block_index == old_index + 1
    assert block.status == 'active'
    blocks = list((await db.execute(select(TrainingBlock).where(
        TrainingBlock.app_user_id == test_user.id).order_by(TrainingBlock.block_index))).scalars())
    assert len(blocks) == 2
    assert blocks[0].id == old_id and blocks[0].status == 'closed'
    assert blocks[1].id == block.id


# --- P0-08, повторное ревью Задачи 7: цепной переход через несколько блоков -


@pytest.mark.asyncio
async def test_chain_rollover_catches_up_to_today(db, test_user: AppUser):
    """Находка 1: пользователь, вернувшийся после отпуска длиной в
    несколько блоков, не должен получить "активный" блок, чей
    planned_end_date всё ещё в прошлом. Один вызов roll_over_if_complete
    перекатывает ровно на длину одного блока (тут — 21 день, 3 фазы по 7
    дней из _seed_periodization); блок, закончившийся на несколько длин
    раньше today, требует НЕСКОЛЬКИХ перекатов подряд за один вызов
    ensure_active_block."""
    await _seed_periodization(db, test_user.id)
    block_length = 21
    # Заведомо больше одной длины блока, но заметно меньше предела
    # MAX_CHAIN_ROLLOVERS (6) — тест проверяет именно цикл, а не выход по
    # исчерпанию попыток (см. test_chain_rollover_gives_up_after_the_cap).
    old_start = TODAY - timedelta(days=3 * block_length + 5)
    old_block = await ensure_active_block(db, test_user.id, old_start)
    old_block_id = old_block.id
    assert TODAY > old_block.planned_end_date, "сценарий должен требовать переката"

    active = await ensure_active_block(db, test_user.id, TODAY)

    assert active.start_date <= TODAY <= active.planned_end_date, (
        "активный блок обязан покрыть today за один вызов ensure_active_block, "
        "даже если разрыв растянулся на несколько длин блока"
    )

    all_blocks = (
        await db.execute(
            select(TrainingBlock)
            .where(TrainingBlock.app_user_id == test_user.id)
            .order_by(TrainingBlock.block_index)
        )
    ).scalars().all()
    closed = [b for b in all_blocks if b.id != active.id]
    assert len(closed) > 1, (
        "разрыв в несколько длин блока обязан потребовать больше одного переката"
    )
    assert len(closed) < params.MAX_CHAIN_ROLLOVERS, (
        "сценарий теста не должен упираться в предел — см. test_chain_rollover_gives_up_after_the_cap"
    )
    assert closed[0].id == old_block_id
    assert all(
        b.status == "closed" and b.close_reason == params.CLOSE_COMPLETED for b in closed
    )
    # Цепочка непрерывна: следующий блок начинается сразу после конца
    # предыдущего, без дыр (то же правило, что и для одиночного переката).
    for prev, nxt in zip(all_blocks, all_blocks[1:]):
        assert nxt.start_date == prev.planned_end_date + timedelta(days=1)
        assert nxt.block_index == prev.block_index + 1
        assert nxt.entry_state == prev.exit_state


@pytest.mark.asyncio
async def test_chain_rollover_gives_up_after_the_cap(db, test_user: AppUser):
    """Находка 1: перерыв длиннее params.MAX_CHAIN_ROLLOVERS блоков подряд —
    восстанавливать цепочку до today дальше бессмысленно, прежняя программа
    потеряла смысл. Ожидание: активный блок стартует СЕГОДНЯ (не тянет
    цепочку дальше предела), а последний закрытый в цепочке имеет причину
    layoff."""
    await _seed_periodization(db, test_user.id)
    block_length = 21
    # Заведомо больше, чем MAX_CHAIN_ROLLOVERS длин блока — цикл обязан
    # остановиться по пределу, а не найти блок, покрывающий today.
    old_start = TODAY - timedelta(days=(params.MAX_CHAIN_ROLLOVERS + 2) * block_length)
    await ensure_active_block(db, test_user.id, old_start)

    active = await ensure_active_block(db, test_user.id, TODAY)

    assert active.status == "active"
    assert active.start_date == TODAY, (
        "после исчерпания MAX_CHAIN_ROLLOVERS перекатов новый блок обязан "
        "начаться сегодня, а не продолжать цепочку дальше"
    )

    all_blocks = (
        await db.execute(
            select(TrainingBlock)
            .where(TrainingBlock.app_user_id == test_user.id)
            .order_by(TrainingBlock.block_index)
        )
    ).scalars().all()
    closed = [b for b in all_blocks if b.id != active.id]
    assert len(closed) == params.MAX_CHAIN_ROLLOVERS + 1, (
        "MAX_CHAIN_ROLLOVERS обычных перекатов + один финальный layoff-закрытие"
    )
    assert all(b.close_reason == params.CLOSE_COMPLETED for b in closed[:-1])
    assert closed[-1].close_reason == params.CLOSE_LAYOFF
    assert active.entry_state == closed[-1].exit_state


# --- Ревью Задачи 6: build_state_snapshot ------------------------------------


@pytest.mark.asyncio
async def test_state_snapshot_captures_progression(db: AsyncSession, test_user: AppUser):
    """До этого теста ни один тест файла не создавал завершённых тренировок,
    поэтому тело цикла build_state_snapshot ни разу не выполнялось — главная
    функция задачи была не проверена вообще."""
    marker = uuid.uuid4().hex[:8]
    exercise = Exercise(
        name=f"Тестовое упражнение снимка {marker}",
        category="base",
        main_muscle_group="chest",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(exercise)
    await db.flush()

    workout = WorkoutSession(
        app_user_id=test_user.id,
        source="free",
        status="finished",
        finished_at=datetime.now(timezone.utc),
    )
    db.add(workout)
    await db.flush()

    se = WorkoutSessionExercise(
        workout_session_id=workout.id, exercise_id=exercise.id, order_index=0
    )
    db.add(se)
    await db.flush()

    for set_number in range(1, 4):
        db.add(
            WorkoutSessionSet(
                workout_session_exercise_id=se.id,
                set_number=set_number,
                set_type="normal",
                weight=60.0,
                reps=8,
                effort_level="medium",
                is_completed=True,
            )
        )
    await db.commit()

    try:
        snapshot = await build_state_snapshot(db, test_user.id, [exercise.id])

        key = str(exercise.id)
        assert key in snapshot
        assert snapshot[key]["completed_sessions"] != 0
        assert snapshot[key]["working_e1rm"] > 0
    finally:
        # Порядок важен (тот же, что в teardown фикстуры test_user в
        # conftest.py): подходы -> упражнения сессии -> сессия -> упражнение.
        await db.execute(
            delete(WorkoutSessionSet).where(
                WorkoutSessionSet.workout_session_exercise_id == se.id
            )
        )
        await db.execute(
            delete(WorkoutSessionExercise).where(WorkoutSessionExercise.id == se.id)
        )
        await db.execute(delete(WorkoutSession).where(WorkoutSession.id == workout.id))
        await db.execute(delete(Exercise).where(Exercise.id == exercise.id))
        await db.commit()


@pytest.mark.asyncio
async def test_state_snapshot_skips_exercises_without_history(
    db: AsyncSession, test_user: AppUser
):
    """Упражнение без единой завершённой сессии не должно попасть в снимок
    вовсе — отсутствие ключа, а не запись с пустыми значениями."""
    marker = uuid.uuid4().hex[:8]
    exercise = Exercise(
        name=f"Тестовое упражнение без истории для снимка {marker}",
        category="base",
        main_muscle_group="back",
        difficulty="beginner",
        equipment_needed=[],
        source="custom",
        app_user_id=test_user.id,
    )
    db.add(exercise)
    await db.commit()

    try:
        snapshot = await build_state_snapshot(db, test_user.id, [exercise.id])
        assert str(exercise.id) not in snapshot
    finally:
        await db.execute(delete(Exercise).where(Exercise.id == exercise.id))
        await db.commit()


# --- Финальное ревью, Находка 1: close_and_advance перечитывает шаблон -------


@pytest.mark.asyncio
async def test_next_block_picks_up_the_edited_template(db, test_user: AppUser):
    """Правка шаблона мезоцикла (спека: «активный блок не меняется, новый
    шаблон применится со следующего») обязана долететь до следующего блока.
    До фикса close_and_advance клонировал phases ЗАКРЫВАЕМОГО блока — то есть
    добавленная сюда фаза не попала бы НИКУДА, начиная со второго блока."""
    await _seed_periodization(db, test_user.id)
    user_meso = (
        await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == test_user.id,
                AppUserMesocycle.is_active.is_(True),
            )
        )
    ).scalars().first()

    block_length = 21  # 3 фазы по 7 дней из _seed_periodization
    old_start = TODAY - timedelta(days=block_length + 4)
    old_block = await ensure_active_block(db, test_user.id, old_start)
    assert [p["effort_tier"] for p in old_block.phases] == ["easy", "medium", "deload"]
    assert TODAY > old_block.planned_end_date, "сценарий должен требовать переката"

    # Пользователь редактирует активный шаблон ПОСЛЕ того, как блок уже создан
    # (ровно сценарий из спеки — правка приходит, пока блок уже идёт).
    db.add(
        MesocyclePhase(
            mesocycle_id=user_meso.mesocycle_id,
            phase_number=4,
            name="extra",
            effort_tier="medium",
        )
    )
    await db.commit()

    active = await ensure_active_block(db, test_user.id, TODAY)

    assert active.id != old_block.id, "сценарий должен закрыть старый блок и открыть следующий"
    assert [p["effort_tier"] for p in active.phases] == [
        "easy", "medium", "deload", "medium",
    ], "следующий блок обязан подхватить ОТРЕДАКТИРОВАННЫЙ шаблон, а не снимок закрытого блока"
    assert active.user_mesocycle_id == user_meso.id
    assert active.mesocycle_id == user_meso.mesocycle_id


@pytest.mark.asyncio
async def test_inserted_deload_does_not_survive_into_the_next_block(db, test_user: AppUser):
    """Досрочная разгрузка, вставленная в блок пользователем, — правка ЭТОГО
    ОДНОГО блока. До фикса close_and_advance клонировал phases закрываемого
    блока целиком, поэтому вставленная разгрузка увековечивалась бы в каждом
    следующем блоке."""
    await _seed_periodization(db, test_user.id)
    block_length = 21
    old_start = TODAY - timedelta(days=block_length + 4)
    old_block = await ensure_active_block(db, test_user.id, old_start)
    assert len(old_block.phases) == 3

    # Симулируем то же самое действие, что и service._perform("insert_deload"):
    # вставляем разгрузку сразу после первой фазы блока.
    updated = phase_ops.insert_deload(
        phase_ops.from_json(old_block.phases),
        after_phase_number=1,
        length_days=old_block.microcycle_length,
    )
    old_block.phases = phase_ops.to_json(updated)
    await db.flush()
    assert len(old_block.phases) == 4, "в закрываемом блоке разгрузка и правда вставлена"
    assert TODAY > old_block.planned_end_date, "сценарий должен требовать переката"

    active = await ensure_active_block(db, test_user.id, TODAY)

    assert active.id != old_block.id
    assert len(active.phases) == 3, (
        "у следующего блока фаз должно быть столько же, сколько в шаблоне — "
        "лишней (вставленной) разгрузки быть не должно"
    )
    assert [p["effort_tier"] for p in active.phases] == ["easy", "medium", "deload"]


@pytest.mark.asyncio
async def test_next_block_falls_back_to_snapshot_without_a_template(db, test_user: AppUser):
    """Активного шаблона нет (пользователь отвязал/деактивировал мезоцикл) —
    следующий блок всё равно обязан появиться, по снимку закрытого блока: без
    фолбэка пользователь остался бы вовсе без активного блока."""
    await _seed_periodization(db, test_user.id)
    block_length = 21
    old_start = TODAY - timedelta(days=block_length + 4)
    old_block = await ensure_active_block(db, test_user.id, old_start)
    old_block_id = old_block.id
    old_user_mesocycle_id = old_block.user_mesocycle_id
    old_mesocycle_id = old_block.mesocycle_id
    assert TODAY > old_block.planned_end_date, "сценарий должен требовать переката"

    user_meso = (
        await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == test_user.id,
                AppUserMesocycle.is_active.is_(True),
            )
        )
    ).scalars().first()
    user_meso.is_active = False
    await db.commit()

    active = await ensure_active_block(db, test_user.id, TODAY)

    assert active is not None, "блок обязан появиться даже без активного шаблона"
    assert active.id != old_block_id
    assert [p["effort_tier"] for p in active.phases] == ["easy", "medium", "deload"], (
        "фолбэк — снимок закрытого блока, а не пустой список"
    )
    assert active.user_mesocycle_id == old_user_mesocycle_id
    assert active.mesocycle_id == old_mesocycle_id
