from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.services.app_user_service import get_current_app_user
from api.services.milestones.service import (
    BodyweightUnknown, LiftNotAvailable, accept_milestone, bodyweight_drift,
    build_showcase,
)
from api.services.models import AppUser

router = APIRouter(prefix="/goals/milestones", tags=["Milestones"])


class MilestoneCardOut(BaseModel):
    code: str
    lift: str
    title: str
    target: float | None
    remaining: float | None
    target_reps: int
    has_history: bool
    suggested_deadline: date | None
    accents: list[str]
    split_requirement: dict
    mesocycle_preset: str


class ShowcaseOut(BaseModel):
    cards: list[MilestoneCardOut]
    active_mesocycle_preset: str | None


@router.get("", response_model=ShowcaseOut)
async def get_milestones(
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    """Витрина вех: ближайшая невзятая и достижимая по каждому движению.

    Код активного пресета мезоцикла едет рядом с карточками — экран
    сравнивает его с `mesocycle_preset` карточки и решает, предложить ли
    переключение (§5.4 шаг 7). Молчаливая замена невозможна: фазы идущего
    блока не переписываются, поэтому это только предложение.
    """
    showcase = await build_showcase(db, current_user.id, date.today())
    return ShowcaseOut(
        cards=[
            MilestoneCardOut(
                code=c.code, lift=c.lift, title=c.title, target=c.target,
                remaining=c.remaining, target_reps=c.target_reps,
                has_history=c.has_history, suggested_deadline=c.suggested_deadline,
                accents=list(c.accents), split_requirement=c.split_requirement,
                mesocycle_preset=c.mesocycle_preset,
            )
            for c in showcase.cards
        ],
        active_mesocycle_preset=showcase.active_mesocycle_preset,
    )


class BodyweightDriftOut(BaseModel):
    goal_id: int
    code: str
    title: str
    stored_bodyweight: float
    current_bodyweight: float
    current_target: float


@router.get("/drift", response_model=BodyweightDriftOut | None)
async def get_drift(
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    """Дрейф веса тела относительно момента принятия вехи, либо null."""
    drift = await bodyweight_drift(db, current_user.id)
    if drift is None:
        return None
    return BodyweightDriftOut(
        goal_id=drift.goal_id, code=drift.code, title=drift.title,
        stored_bodyweight=drift.stored_bodyweight,
        current_bodyweight=drift.current_bodyweight,
        current_target=drift.current_target,
    )


@router.post("/{code}/accept", status_code=status.HTTP_201_CREATED)
async def accept(
    code: str,
    db: AsyncSession = Depends(get_db),
    current_user: AppUser = Depends(get_current_app_user),
):
    """Принять веху: создать ведущую цель и применить дешёвые рычаги."""
    try:
        goal = await accept_milestone(db, current_user.id, code, date.today())
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Веха не найдена")
    except LiftNotAvailable:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Упражнение недоступно")
    except BodyweightUnknown:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Укажите вес тела — без него порог этой вехи не посчитать",
        )
    await db.commit()

    # P0-12: принятие вехи меняет ведущую цель — тот же случай, что и
    # создание/правка цели (api/routers/goals.py) или смена входов плана
    # (api/routers/profile.py), автопилот пересчитывает предложение немедленно.
    # refresh_goal_proposals коммитит СЕБЯ САМА на каждом пути записи (см. её
    # докстринг, финальное ревью Important 9) — второго commit() здесь не
    # нужен и не нужен после этого вызова.
    from api.services.goal.service import refresh_goal_proposals
    from api.services.volume.repository import guarded, utc_today

    await guarded(
        db, "обновление автопилота цели",
        refresh_goal_proposals(db, current_user.id, utc_today()),
    )

    return {"goal_id": goal.id, "target_value": goal.target_value}
