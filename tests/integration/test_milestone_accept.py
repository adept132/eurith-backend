"""Принятие вехи: ведущая цель, дешёвые рычаги, память о весе (§5.4)."""
import pytest
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from api.services.milestones.repository import resolve_lift_exercises
from api.services.models import (
    AppUserProfile, PeriodizationProposal, TrainingBlock, UserAnthropometry,
    UserExercisePreference, UserGoal,
)
from api.services.periodization import params as periodization_params
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _prepare(user_id: int, kg: float | None = 80.0) -> None:
    async with SessionLocal() as db:
        if kg is not None:
            db.add(UserAnthropometry(
                app_user_id=user_id, weight=kg,
                recorded_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            ))
        db.add(AppUserProfile(app_user_id=user_id, experience_level="intermediate",
                              settings={}))
        await db.commit()


async def test_accept_creates_a_primary_strength_goal(client, auth_headers, test_user):
    await _prepare(test_user.id)

    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 201, r.text

    async with SessionLocal() as db:
        goals = (await db.execute(
            select(UserGoal).where(UserGoal.app_user_id == test_user.id)
        )).scalars().all()
    assert len(goals) == 1
    goal = goals[0]
    assert goal.goal_type == "strength"
    assert goal.is_primary is True
    assert goal.target_value == 160.0, "двойной вес при 80 кг"
    assert goal.target_reps == 1


async def test_accept_marks_the_lift_as_favorite(client, auth_headers, test_user):
    """Дешёвый рычаг: тот же носитель, что у LEVER_ENSURE_PRESENT в P0-12."""
    await _prepare(test_user.id)
    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 201, r.text

    async with SessionLocal() as db:
        resolved = await resolve_lift_exercises(db)
        prefs = (await db.execute(
            select(UserExercisePreference).where(
                UserExercisePreference.app_user_id == test_user.id,
                UserExercisePreference.exercise_id == resolved["squat"],
            )
        )).scalars().all()
    assert [p.preference for p in prefs] == ["favorite"]


async def test_accept_remembers_the_rule_and_the_bodyweight(client, auth_headers, test_user):
    """§5.4 шаг 4: без веса на момент принятия строку дрейфа не показать."""
    await _prepare(test_user.id, kg=80.0)
    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 201, r.text

    async with SessionLocal() as db:
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalars().first()
    stored = (profile.settings or {}).get("milestone") or {}
    assert stored.get("code") == "squat_2x_bw"
    assert stored.get("bodyweight") == 80.0


async def test_accepting_a_second_milestone_replaces_the_primary(client, auth_headers, test_user):
    """Структурные рычаги не делятся между двумя хозяевами (решение 2 P0-12)."""
    await _prepare(test_user.id)
    assert (await client.post("/goals/milestones/squat_2x_bw/accept",
                              headers=auth_headers)).status_code == 201
    assert (await client.post("/goals/milestones/bench_100kg/accept",
                              headers=auth_headers)).status_code == 201

    async with SessionLocal() as db:
        primary = (await db.execute(
            select(UserGoal).where(
                UserGoal.app_user_id == test_user.id, UserGoal.is_primary.is_(True),
            )
        )).scalars().all()
    assert len(primary) == 1
    assert primary[0].target_value == 100.0, "ведущей стала сотка в жиме"


async def test_relative_milestone_without_bodyweight_is_refused(client, auth_headers, test_user):
    """Порог посчитать нечем — 409, а не цель с выдуманным числом."""
    await _prepare(test_user.id, kg=None)

    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 409, r.text

    async with SessionLocal() as db:
        goals = (await db.execute(
            select(UserGoal).where(UserGoal.app_user_id == test_user.id)
        )).scalars().all()
    assert goals == [], "отказ не должен оставлять недоделанную цель"


async def test_absolute_milestone_works_without_bodyweight(client, auth_headers, test_user):
    """Сотка от веса тела не зависит и принимается без него."""
    await _prepare(test_user.id, kg=None)
    r = await client.post("/goals/milestones/bench_100kg/accept", headers=auth_headers)
    assert r.status_code == 201, r.text


async def test_pullup_without_bodyweight_is_still_refused_on_accept(client, auth_headers, test_user):
    """Финальное ревью, Important 5: подтягивание видно на витрине и без
    веса тела (visibility.py), но порог этой вехи по-прежнему кратен весу
    тела — принять её и создать цель с выдуманным числом нельзя, отказ
    обязан остаться отказом, как у любой другой относительной вехи."""
    await _prepare(test_user.id, kg=None)
    r = await client.post("/goals/milestones/pullup_first/accept", headers=auth_headers)
    assert r.status_code == 409, r.text

    async with SessionLocal() as db:
        goals = (await db.execute(
            select(UserGoal).where(UserGoal.app_user_id == test_user.id)
        )).scalars().all()
    assert goals == [], "отказ не должен оставлять недоделанную цель"


async def test_accept_wakes_the_goal_autopilot(client, auth_headers, test_user, monkeypatch):
    """Финальное ревью, Important 1: принятие вехи меняет ведущую цель — та
    же дисциплина, что и у создания/правки цели (api/routers/goals.py) и
    смены входов плана (api/routers/profile.py): refresh_goal_proposals
    обязана позваться немедленно, а не молчать до постороннего запроса."""
    await _prepare(test_user.id)

    called = False

    async def _spy(session, app_user_id, today):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr("api.services.goal.service.refresh_goal_proposals", _spy)

    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 201, r.text
    assert called, "автопилот обязан пересчитаться сразу же, а не молчать"


async def test_accept_survives_autopilot_crash(client, auth_headers, test_user, monkeypatch):
    """guarded() не должен позволить упавшему решателю уронить сам accept —
    та же живучесть, что и у соседних точек (test_goal_refresh_points.py)."""
    await _prepare(test_user.id)

    async def _boom(*args, **kwargs):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr("api.services.goal.service.refresh_goal_proposals", _boom)

    r = await client.post("/goals/milestones/squat_2x_bw/accept", headers=auth_headers)
    assert r.status_code == 201, r.text


async def test_accept_without_a_profile_creates_one_and_remembers_the_milestone(
    client, auth_headers, test_user,
):
    """Финальное ревью, Important 4: раньше accept_milestone писала память о
    вехе только `if profile is not None` — у человека без AppUserProfile не
    сохранялось ничего, и молча отваливались и строка дрейфа
    (bodyweight_drift), и акценты в генераторе (milestone_accents). Без
    _prepare — этот тест намеренно не заводит профиль заранее; сотка в жиме
    от веса тела не зависит, так что не нужна и запись UserAnthropometry.
    """
    r = await client.post("/goals/milestones/bench_100kg/accept", headers=auth_headers)
    assert r.status_code == 201, r.text

    async with SessionLocal() as db:
        profile = (await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == test_user.id)
        )).scalars().first()
    assert profile is not None, "профиль обязан завестись"
    stored = (profile.settings or {}).get("milestone") or {}
    assert stored.get("code") == "bench_100kg"


async def test_unknown_code_gives_404(client, auth_headers, test_user):
    await _prepare(test_user.id)
    r = await client.post("/goals/milestones/no-such-milestone/accept", headers=auth_headers)
    assert r.status_code == 404, r.text


# --- Смена ведущей цели гасит рычаг прежней (docstring accept_milestone) ---
#
# accept_milestone экспирует pending goal_plan-предложения СНЯТОЙ ведущей
# цели: apply_decision (periodization/service.py) не перепроверяет, что
# goal_id из payload всё ещё ведущая, поэтому непогашенный рычаг снятой цели
# можно применить к чужому упражнению. Ни один из тестов выше это не трогал.

async def _make_block(app_user_id: int) -> int:
    """Минимальный блок ради FK PeriodizationProposal.block_id — по образцу
    test_incomplete_evaluation_leaves_pending_proposal_untouched
    (test_goal_proposal.py): статус блока принятию вехи не важен."""
    async with SessionLocal() as db:
        block = TrainingBlock(
            app_user_id=app_user_id,
            block_index=1,
            phases=[{"phase_number": 1, "name": "medium", "effort_tier": "medium", "length_days": 7}],
            microcycle_length=7,
            start_date=date.today() - timedelta(days=30),
            planned_end_date=date.today() + timedelta(days=10),
            status="active",
        )
        db.add(block)
        await db.commit()
        await db.refresh(block)
        return block.id


async def _make_pending_goal_plan(
    *, app_user_id: int, block_id: int, goal_id: int, exercise_id: int,
) -> int:
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=app_user_id,
            block_id=block_id,
            kind=periodization_params.KIND_GOAL_PLAN,
            reason_code="pace_behind",
            payload={"goal_id": goal_id, "exercise_id": exercise_id, "inputs_hash": "old"},
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        return proposal.id


async def test_accepting_a_new_milestone_expires_previous_primarys_pending_proposal(
    client, auth_headers, test_user,
):
    """Рычаг снятой цели (favorite-упражнение, override схемы, перегенерация
    календаря) не должен пережить смену ведущей цели ни на секунду дольше,
    чем сама цель — иначе apply_decision применит его не к тому упражнению."""
    await _prepare(test_user.id)
    assert (await client.post(
        "/goals/milestones/squat_2x_bw/accept", headers=auth_headers
    )).status_code == 201

    async with SessionLocal() as db:
        goal_a = (await db.execute(
            select(UserGoal).where(
                UserGoal.app_user_id == test_user.id, UserGoal.is_primary.is_(True),
            )
        )).scalars().one()
        goal_a_id, squat_exercise_id = goal_a.id, goal_a.exercise_id

    block_id = await _make_block(test_user.id)
    proposal_id = await _make_pending_goal_plan(
        app_user_id=test_user.id, block_id=block_id,
        goal_id=goal_a_id, exercise_id=squat_exercise_id,
    )

    assert (await client.post(
        "/goals/milestones/bench_100kg/accept", headers=auth_headers
    )).status_code == 201

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, proposal_id)
    assert proposal.status == periodization_params.STATUS_EXPIRED


async def test_accepting_a_new_milestone_leaves_unrelated_proposals_pending(
    client, auth_headers, test_user,
):
    """Фильтр, гасящий лишнее (все pending пользователя, а не только
    прежней ведущей цели), — такой же дефект, как фильтр, не гасящий нужное."""
    await _prepare(test_user.id)
    assert (await client.post(
        "/goals/milestones/squat_2x_bw/accept", headers=auth_headers
    )).status_code == 201

    async with SessionLocal() as db:
        goal_a = (await db.execute(
            select(UserGoal).where(
                UserGoal.app_user_id == test_user.id, UserGoal.is_primary.is_(True),
            )
        )).scalars().one()
        goal_a_id = goal_a.id
        resolved = await resolve_lift_exercises(db)

    block_id = await _make_block(test_user.id)
    # Предложение прежней ведущей — обязано погаснуть.
    expired_id = await _make_pending_goal_plan(
        app_user_id=test_user.id, block_id=block_id,
        goal_id=goal_a_id, exercise_id=resolved["squat"],
    )
    # Предложение ЧУЖОЙ цели того же пользователя (другой exercise_id, чтобы
    # не столкнуться с uq_periodization_proposals_pending) — обязано остаться.
    untouched_id = await _make_pending_goal_plan(
        app_user_id=test_user.id, block_id=block_id,
        goal_id=goal_a_id + 10_000_000, exercise_id=resolved["bench"],
    )

    assert (await client.post(
        "/goals/milestones/bench_100kg/accept", headers=auth_headers
    )).status_code == 201

    async with SessionLocal() as db:
        expired = await db.get(PeriodizationProposal, expired_id)
        untouched = await db.get(PeriodizationProposal, untouched_id)
    assert expired.status == periodization_params.STATUS_EXPIRED
    assert untouched.status == periodization_params.STATUS_PENDING


# --- Принятие вехи закрывает висящее «выбери следующую» (финальное ревью, Important 3) ---
#
# KIND_GOAL_NEXT рождается при закрытии ведущей цели (_retire_finished_
# primary_goal, goal/service.py) и раньше умел уходить с pending только по
# кнопке «Не сейчас» — принятие новой ведущей цели через веху его не
# трогало, и предложение копилось по одному на каждую закрытую цель.

async def _make_pending_goal_next(*, app_user_id: int, block_id: int, closed_goal_id: int) -> int:
    async with SessionLocal() as db:
        proposal = PeriodizationProposal(
            app_user_id=app_user_id,
            block_id=block_id,
            kind=periodization_params.KIND_GOAL_NEXT,
            reason_code=periodization_params.REASON_GOAL_ACHIEVED,
            payload={"closed_goal_id": closed_goal_id},
            status=periodization_params.STATUS_PENDING,
        )
        db.add(proposal)
        await db.commit()
        await db.refresh(proposal)
        return proposal.id


async def test_accepting_a_milestone_closes_the_pending_goal_next_proposal(
    client, auth_headers, test_user,
):
    """Спека требует pending -> accepted, когда человек создал новую ведущую
    цель — веха тоже создаёт ведущую цель и обязана гасить карточку так же,
    как это уже делает apply_decision по кнопке."""
    await _prepare(test_user.id)
    block_id = await _make_block(test_user.id)
    proposal_id = await _make_pending_goal_next(
        app_user_id=test_user.id, block_id=block_id, closed_goal_id=1,
    )

    assert (await client.post(
        "/goals/milestones/squat_2x_bw/accept", headers=auth_headers
    )).status_code == 201

    async with SessionLocal() as db:
        proposal = await db.get(PeriodizationProposal, proposal_id)
    assert proposal.status == periodization_params.STATUS_ACCEPTED
    assert proposal.status != periodization_params.STATUS_PENDING
