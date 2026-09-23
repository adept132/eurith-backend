"""Закрытие ведущей цели рождает предложение выбрать следующую (§5.6)."""
import pytest
from datetime import date, timedelta

import pytest_asyncio
from sqlalchemy import select

from api.seed_splits import ensure_system_splits
from api.services.periodization import params as periodization_params
from api.services.periodization.repository import ensure_active_block
from api.services.models import (
    PeriodizationProposal,
    UserExerciseProgressionState,
    UserGoal,
)
from api.services.structure.bootstrap import ensure_structure
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def seeded_block(test_user):
    async with SessionLocal() as db:
        await ensure_system_splits(db)
        await db.commit()
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()
    async with SessionLocal() as db:
        block = await ensure_active_block(db, test_user.id, date.today())
    assert block is not None, "без блока предложению некуда привязаться"
    return block


async def test_kind_and_reasons_are_declared():
    assert periodization_params.KIND_GOAL_NEXT == "goal_next"
    assert periodization_params.REASON_GOAL_ACHIEVED
    assert periodization_params.REASON_GOAL_OVERDUE
    texts = periodization_params.REASON_TEXTS
    assert periodization_params.REASON_GOAL_ACHIEVED in texts
    assert periodization_params.REASON_GOAL_OVERDUE in texts


async def test_overdue_primary_goal_produces_a_proposal(test_user, seeded_block):
    """Срок вышел — слот ведущей освобождается, и человеку предлагают следующую."""
    async with SessionLocal() as db:
        goal = UserGoal(
            app_user_id=test_user.id, goal_type="strength", target_value=200.0,
            target_reps=1, exercise_id=None, is_primary=True,
            deadline=date.today() - timedelta(days=1),
        )
        db.add(goal)
        await db.commit()
        goal_id = goal.id

    from api.services.goal.service import refresh_goal_proposals
    async with SessionLocal() as db:
        await refresh_goal_proposals(db, test_user.id, date.today())

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.app_user_id == test_user.id,
                PeriodizationProposal.kind == periodization_params.KIND_GOAL_NEXT,
            )
        )).scalars().all()
        goal_row = (await db.execute(
            select(UserGoal).where(UserGoal.id == goal_id)
        )).scalars().first()

    assert len(rows) == 1
    assert rows[0].reason_code == periodization_params.REASON_GOAL_OVERDUE
    assert rows[0].payload["closed_goal_id"] == goal_id
    assert goal_row.is_primary is False, "слот ведущей обязан освободиться"


async def test_achieved_primary_goal_produces_a_proposal_with_achieved_reason(
    test_user, seeded_block, fresh_exercise
):
    """Рабочий e1RM дошёл до целевого — карточка обязана нести причину
    «достигнута», а не «просрочена» (дедлайн намеренно в будущем, чтобы
    ветка просрочки не могла сработать и подменить причину).

    target_reps=1, target_value=100.0 -> target_e1rm = 100*(1+1/30) ≈ 103.33
    (та же формула Эпли, что в _goal_target_e1rm). working_e1rm=110 заведомо
    выше порога.
    """
    async with SessionLocal() as db:
        goal = UserGoal(
            app_user_id=test_user.id, goal_type="strength", target_value=100.0,
            target_reps=1, exercise_id=fresh_exercise.id, is_primary=True,
            deadline=date.today() + timedelta(days=60),
        )
        db.add(goal)
        await db.commit()
        goal_id = goal.id

        db.add(UserExerciseProgressionState(
            app_user_id=test_user.id, exercise_id=fresh_exercise.id, working_e1rm=110.0,
        ))
        await db.commit()

    from api.services.goal.service import refresh_goal_proposals
    async with SessionLocal() as db:
        await refresh_goal_proposals(db, test_user.id, date.today())

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.app_user_id == test_user.id,
                PeriodizationProposal.kind == periodization_params.KIND_GOAL_NEXT,
            )
        )).scalars().all()
        goal_row = (await db.execute(
            select(UserGoal).where(UserGoal.id == goal_id)
        )).scalars().first()

    assert len(rows) == 1
    assert rows[0].reason_code == periodization_params.REASON_GOAL_ACHIEVED
    assert rows[0].payload["closed_goal_id"] == goal_id
    assert goal_row.is_primary is False, "слот ведущей обязан освободиться"


async def test_proposal_is_not_duplicated_on_repeated_refresh(test_user, seeded_block):
    """Повторный прогон refresh_goal_proposals на уже закрытой цели не плодит
    вторую карточку goal_next.

    ВАЖНО: защиту от дублей здесь на самом деле обеспечивает не проверка
    payload["closed_goal_id"] в блоке создания карточки, а то, что
    _retire_finished_primary_goal снимает is_primary с цели ДО этого блока —
    вторым прогоном _primary_goal её уже не находит, и функция выходит
    раньше, чем дошла бы до проверки на дубль. Сама проверка — страховка на
    случай гонки параллельных запросов до коммита первого, и её всё равно
    нельзя убирать.
    """
    async with SessionLocal() as db:
        db.add(UserGoal(
            app_user_id=test_user.id, goal_type="strength", target_value=200.0,
            target_reps=1, exercise_id=None, is_primary=True,
            deadline=date.today() - timedelta(days=1),
        ))
        await db.commit()

    from api.services.goal.service import refresh_goal_proposals
    for _ in range(3):
        async with SessionLocal() as db:
            await refresh_goal_proposals(db, test_user.id, date.today())

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(PeriodizationProposal).where(
                PeriodizationProposal.app_user_id == test_user.id,
                PeriodizationProposal.kind == periodization_params.KIND_GOAL_NEXT,
            )
        )).scalars().all()
    assert len(rows) == 1, "повторный прогон не должен плодить карточки"
