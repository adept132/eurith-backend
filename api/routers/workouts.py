from __future__ import annotations

from uuid import uuid4
from api.services.analytics.writes import apply_field, apply_set_context, fingerprint, json_value
from api.services.analytics.state import mark_dirty
from api.services.analytics.config import analytics_enabled
from api.services.analytics.snapshots import capture_workout_snapshot
from api.schemas.workouts import UpdateAnalyticsContextRequest
from copy import deepcopy
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.deps import get_db
from api.schemas.warmups import snapshot_json, validate_warmup_rows
from api.services.workout_tombstones import record_child_deletion
from api.schemas.supersets import CreateSupersetRequest, ReorderWorkoutStructureRequest
from api.schemas.workouts import WorkoutSessionDetailResponse, AddWorkoutExerciseRequest, AddWorkoutSetResponse, \
    AddWorkoutSetRequest, UpdateWorkoutSetRequest, RepeatWorkoutSetRequest, AutoprogressionResponse
from api.services.anomaly_stats import review_set, review_workout
from api.services.app_user_service import get_current_app_user
from api.services.autoprogression import compute_autoprogression
from api.services.calculate_exercise_recommendation import calculate_exercise_recommendations
from api.services.exercise_utils import get_base_exercise_query
from api.services.progression import params as progression_params
from api.services.progression import repository as progression_repo
from api.services.progression.engine import plan_exercise
from api.services.progression.resolve import override_for
from api.services.readiness import repository as readiness_repo
from api.services.workout_superset_service import WorkoutSupersetService
from api.services.workout_set_context import CONTEXT_KEYS, resolve_set_context
from api.services.load_context import resolve_load_context
from api.services.models import WorkoutSession, WorkoutSessionExercise, Exercise, WorkoutSessionSet, AppUser, \
    AppUserProfile, GymProfile, GymExerciseSetup, ExerciseLoadPreference

router = APIRouter(tags=["workouts"])


def _workout_session_options():
    """Загружает упражнения сессии, а также связанные с ними данные (каталог и подходы) за один проход."""
    return selectinload(WorkoutSession.exercises).options(
        selectinload(WorkoutSessionExercise.exercise),
        selectinload(WorkoutSessionExercise.sets)
    )

def _renumber_sets(session_exercise: WorkoutSessionExercise) -> None:
    ordered_sets = sorted(session_exercise.sets, key=lambda s: s.set_number)
    for index, workout_set in enumerate(ordered_sets, start=1):
        workout_set.set_number = index


@router.get(
    "/workouts/active",
    response_model=WorkoutSessionDetailResponse,
)
async def get_active_workout(
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    workout = await get_active_workout_for_user(db, current_app_user.id)

    if workout is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active workout",
        )

    return workout

@router.get(
    "/workouts/{workout_id}",
    response_model=WorkoutSessionDetailResponse,
)
async def get_workout_detail(
    workout_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    print("GET DETAIL INPUT:", {
        "workout_id": workout_id,
        "current_app_user_id": current_app_user.id,
    })

    raw_stmt = select(WorkoutSession).where(WorkoutSession.id == workout_id)
    raw_result = await db.execute(raw_stmt)
    raw_workout = raw_result.scalar_one_or_none()

    print("RAW WORKOUT:", raw_workout)
    if raw_workout is not None:
        print("RAW WORKOUT DATA:", {
            "id": raw_workout.id,
            "app_user_id": raw_workout.app_user_id,
            "source": raw_workout.source,
            "status": raw_workout.status,
            "split_day_id": raw_workout.split_day_id,
        })

    stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.id == workout_id,
            WorkoutSession.app_user_id == current_app_user.id,
        )
        # === ИСПОЛЬЗУЙ ЭТОТ БЛОК ВМЕСТО СТАРЫХ ФУНКЦИЙ ===
        .options(
            _workout_session_options()
        )
    )

    result = await db.execute(stmt)
    workout = result.scalar_one_or_none()

    print("FILTERED WORKOUT:", workout)

    if workout is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout not found",
        )

    return workout


@router.delete(
    "/workouts/{workout_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workout(
    workout_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    """Отмена тренировки: каскадно удаляет упражнения и подходы сессии."""
    stmt = select(WorkoutSession).where(WorkoutSession.id == workout_id)
    result = await db.execute(stmt)
    workout = result.scalar_one_or_none()

    if workout is None or workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout not found",
        )

    await db.delete(workout)
    await mark_dirty(db, current_app_user.id)
    await db.commit()


@router.post(
    "/workouts/{workout_id}/exercises",
    response_model=WorkoutSessionDetailResponse,
)
async def add_exercise_to_workout(
        workout_id: int,
        payload: AddWorkoutExerciseRequest,
        db: AsyncSession = Depends(get_db),
        current_app_user=Depends(get_current_app_user),
):
    workout_stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.id == workout_id,
            WorkoutSession.app_user_id == current_app_user.id,
        )
        .options(
            selectinload(WorkoutSession.exercises),
        )
    )

    workout_result = await db.execute(workout_stmt)
    workout = workout_result.scalar_one_or_none()

    if workout is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    exercise_stmt = get_base_exercise_query(current_app_user.id).where(Exercise.id == payload.exercise_id)
    exercise_result = await db.execute(exercise_stmt)
    exercise = exercise_result.scalar_one_or_none()

    if exercise is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Exercise not found",
        )

    existing_session_exercise = next(
        (item for item in workout.exercises if item.exercise_id == payload.exercise_id),
        None,
    )

    if existing_session_exercise is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Exercise already added to this workout",
        )

    # --- ИСПРАВЛЕННАЯ ЛОГИКА РЕОРДЕРА ---

    # 1. Надежное вычисление индекса для автоматического добавления в конец.
    if workout.exercises:
        max_order = max((ex.order_index for ex in workout.exercises if ex.order_index is not None), default=-1)
        next_order_index = max_order + 1
    else:
        next_order_index = 0

    fatigue_tier = getattr(exercise, "fatigue_tier", 2)
    recommendations = await calculate_exercise_recommendations(
        db, current_app_user.id, single_exercise_id=exercise.id, single_fatigue_tier=fatigue_tier
    )
    rec_data = recommendations[0] if recommendations else {}

    session_exercise = WorkoutSessionExercise(
        workout_session_id=workout.id,
        exercise_id=payload.exercise_id,
        order_index=next_order_index,
        superset_group=payload.superset_group,
        notes=payload.notes,
        warmup_group=snapshot_json(payload.warmup_group),
        # ДОБАВЛЯЕМ СНИМКИ
        # (фикс несовпадения ключей: calculate_exercise_recommendations возвращает
        # "recommended_rir"/"recommended_rep_min"/"recommended_rep_max", а не "rir"/"rmin"/"rmax" —
        # из-за этого поля всегда были null, и автопрогрессия ниже не имела rep_candidates)
        recommended_rir=rec_data.get("recommended_rir"),
        recommended_rep_min=rec_data.get("recommended_rep_min"),
        recommended_rep_max=rec_data.get("recommended_rep_max"),
    )

    apply_field(session_exercise, payload, "analytics_load_defaults")
    db.add(session_exercise)
    await db.flush()

    # Рекомендованный вес заполняем при добавлении упражнения, чтобы накапливать
    # объективную метку «предписано против сделано» (см. spec §8.3).
    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_app_user.id)
    )
    profile = profile_result.scalars().first()
    experience_level = profile.experience_level if profile else None
    settings = profile.settings if profile else None

    # exercise уже загружен выше — присваиваем явно, чтобы compute_autoprogression
    # не пытался сделать ленивую подгрузку session_exercise.exercise (запрещено в async SQLAlchemy).
    session_exercise.exercise = exercise

    # Фаза мезоцикла нужна схеме percent_1rm и правилу deload_phase (P0-06 C2) —
    # без неё они мёртвый код на этом пишущем пути, как и было до фикса.
    phase_effort_tier = await progression_repo.resolve_phase_effort_tier(
        db, workout.app_user_mesocycle_id, workout.mesocycle_phase,
        training_block_id=workout.training_block_id,
    )

    readiness_verdict = await readiness_repo.verdict_for_checkin(
        db, current_app_user.id, payload.readiness_checkin_uuid, settings
    )

    # Предписание считаем сразу: пользователь должен увидеть цель до первого
    # подхода, а evaluate() потом сравнит факт именно с ней.
    ctx = await progression_repo.build_context(
        db,
        session_exercise,
        current_app_user.id,
        experience_level,
        settings,
        # P0-06 C2: настоящий источник диапазона повторов, а не дефолт
        # build_context — calculate_exercise_recommendations уже посчитал
        # его выше (rec_data), раньше значение молча отбрасывалось.
        rep_range_source=rec_data.get("rep_range_source", progression_params.REP_SOURCE_FALLBACK),
        phase_effort_tier=phase_effort_tier,
        readiness=readiness_verdict,
    )
    prescription = plan_exercise(
        ctx, override=override_for(settings, session_exercise.exercise_id)
    )
    progression_repo.persist_prescription(session_exercise, prescription)
    await mark_dirty(db, current_app_user.id)

    await db.commit()

    detail_stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.id == workout_id,
            WorkoutSession.app_user_id == current_app_user.id,
        )
        .options(
            _workout_session_options()
        )
        # populate_existing: `workout` (тот же объект, что вернёт этот запрос —
        # SQLAlchemy identity map) уже был загружен ВЫШЕ с selectinload(exercises),
        # который в момент загрузки был пуст (упражнение ещё не добавлено). Раз
        # коллекция уже помечена загруженной, а expire_on_commit=False (см.
        # app/database.py), commit() её не инвалидирует — без populate_existing
        # повторный SELECT ниже молча вернул бы тот же устаревший пустой список
        # вместо только что добавленного упражнения.
        .execution_options(populate_existing=True)
    )

    detail_result = await db.execute(detail_stmt)
    updated_workout = detail_result.scalar_one()

    return updated_workout


@router.post(
    "/workout-session-exercises/{session_exercise_id}/sets",
    response_model=AddWorkoutSetResponse,
)
async def add_set_to_session_exercise(
    session_exercise_id: int,
    payload: AddWorkoutSetRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    print("ADD SET PAYLOAD:", payload.model_dump())

    stmt = (
        select(WorkoutSessionExercise)
        .where(WorkoutSessionExercise.id == session_exercise_id)
        .options(
            selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await db.execute(stmt)
    session_exercise = result.scalar_one_or_none()

    if session_exercise is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    workout = session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    if payload.parent_set_id is not None:
        parent_exists = any(s.id == payload.parent_set_id for s in session_exercise.sets)
        if not parent_exists:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent set does not belong to this exercise",
            )

    if payload.warmup_snapshot is not None:
        try:
            validate_warmup_rows(session_exercise.warmup_group,
                [s.warmup_snapshot for s in session_exercise.sets] + [payload.warmup_snapshot])
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    requested = payload.model_fields_set
    mode = payload.load_mode if "load_mode" in requested else session_exercise.active_load_mode
    gym_id = (payload.gym_profile_id if "gym_profile_id" in requested
              else workout.gym_profile_id)
    setup_id = (payload.setup_id if "setup_id" in requested
                else session_exercise.active_setup_id)
    if "load_mode" in requested and mode != session_exercise.active_load_mode and "setup_id" not in requested:
        setup_id = None
    if "gym_profile_id" in requested and gym_id != workout.gym_profile_id and "setup_id" not in requested:
        setup_id = None
    try:
        context = await resolve_set_context(
            db, current_app_user.id, session_exercise, mode=mode, gym_id=gym_id,
            setup_id=setup_id, supplied_snapshot=payload.load_snapshot,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    next_set_number = len(session_exercise.sets) + 1

    new_set = WorkoutSessionSet(
        workout_session_exercise_id=session_exercise.id,
        set_number=next_set_number,
        set_type=payload.set_type,
        weight=payload.weight,
        reps=payload.reps,
        effort_level=payload.effort_level,
        notes=payload.notes,
        parent_set_id=payload.parent_set_id,
        superset_round=payload.superset_round,
        is_completed=True,
        is_max_reps=payload.is_max_reps,
        is_anomalous=False,
        **context,
        shown_target_snapshot=deepcopy(payload.shown_target_snapshot),
        warmup_snapshot=snapshot_json(payload.warmup_snapshot),
    )

    print(
        "NEW SET VALUES:",
        {
            "set_number": next_set_number,
            "weight": payload.weight,
            "reps": payload.reps,
            "effort_level": payload.effort_level,
        },
    )

    if analytics_enabled() or session_exercise.analytics_load_defaults is not None:
        new_set.client_uuid = str(uuid4())
    apply_set_context(new_set, payload, session_exercise, is_new=True)
    await review_set(db, workout, session_exercise, new_set, confirmed=payload.anomaly_confirmed if 'anomaly_confirmed' in payload.model_fields_set else None)
    db.add(new_set)
    await mark_dirty(db, current_app_user.id)
    await db.commit()
    await db.refresh(new_set)

    return new_set

@router.get(
    "/workout-session-exercises/{session_exercise_id}/autoprogression",
    response_model=AutoprogressionResponse,
)
async def get_exercise_autoprogression(
    session_exercise_id: int,
    target_reps: int | None = Query(default=None, gt=0),
    target_effort: str | None = Query(default=None),
    load_mode: Literal["stack", "plate_loaded"] | None = Query(default=None),
    setup_id: UUID | None = Query(default=None),
    gym_profile_id: UUID | Literal["none"] | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    """Рекомендация автопрогрессии для упражнения в текущей сессии.

    target_reps/target_effort передаются только в свободной тренировке (пользователь
    выбирает их вручную). В плановой берём целевые параметры из сессии.
    """
    stmt = (
        select(WorkoutSessionExercise)
        .where(WorkoutSessionExercise.id == session_exercise_id)
        .options(
            selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionExercise.exercise),
        )
    )
    result = await db.execute(stmt)
    session_exercise = result.scalar_one_or_none()

    if session_exercise is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    if session_exercise.workout_session.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    profile_result = await db.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == current_app_user.id)
    )
    profile = profile_result.scalars().first()
    experience_level = profile.experience_level if profile else None
    settings = profile.settings if profile else None

    load_context = None
    if load_mode is None and (setup_id is not None or gym_profile_id is not None):
        raise HTTPException(status_code=400, detail="Load mode is required for machine context")
    if load_mode is not None:
        # Omitted query keeps the persisted workout context for older clients;
        # explicit "none" reflects a local no-gym choice before workout sync.
        selected_gym_id = (
            None if gym_profile_id == "none"
            else gym_profile_id if gym_profile_id is not None
            else session_exercise.workout_session.gym_profile_id
        )
        gym = await db.get(GymProfile, selected_gym_id) if selected_gym_id is not None else None
        if selected_gym_id is not None and (
            gym is None or gym.app_user_id != current_app_user.id or gym.deleted_at is not None
        ):
            raise HTTPException(status_code=400, detail="Gym profile is unavailable")
        catalog = session_exercise.exercise
        source = "user" if catalog.app_user_id is not None else "global"
        preference = (await db.execute(select(ExerciseLoadPreference).where(
            ExerciseLoadPreference.app_user_id == current_app_user.id,
            ExerciseLoadPreference.exercise_source == source,
            ExerciseLoadPreference.exercise_id == catalog.id,
        ))).scalar_one_or_none()
        setup = await db.get(GymExerciseSetup, setup_id) if setup_id is not None else None
        if setup_id is not None and (
            setup is None or gym is None or setup.gym_id != gym.id
            or setup.exercise_source != source or setup.exercise_id != catalog.id
            or setup.load_mode != load_mode or setup.deleted_at is not None
            or not setup.is_available
        ):
            raise HTTPException(status_code=400, detail="Machine setup does not match this exercise and gym")
        load_context = resolve_load_context(
            catalog.equipment_needed or [], preference, gym,
            [setup] if setup is not None else [], load_mode, settings,
        )
        if load_context is None:
            raise HTTPException(status_code=400, detail="Load mode is not allowed for this exercise")

    # Фаза мезоцикла нужна схеме percent_1rm и правилу deload_phase, но обе
    # сравнивают со строковым effort_tier ("prefailure"/"failure"/"deload"/...),
    # а не с номером фазы. WorkoutSession.mesocycle_phase — это НОМЕР (int), не
    # название (см. развёрнутый комментарий в
    # api/services/progression/repository.py::_phase_effort_tier_stmt) — просто
    # str(...) от него дал бы "1"/"2"/... и обе проверки молча никогда бы не
    # сработали. P0-06 C2: резолв фазы вынесен в репозиторий (единая точка
    # для этого read-only пути и всех пишущих), этот эндпоинт был одной из
    # двух точек дублирования join'а — теперь их не осталось.
    # P0-08, ревью Задачи 8, находка 1: предпросмотр обязан читать тот же
    # источник фазы, что и пишущие пути (снимок блока приоритетнее шаблона) —
    # иначе на живом пересчёте (target_reps или ещё не сохранённое
    # предписание) вставленная разгрузка не срежет вес, хотя на остальных
    # трёх точках (создание сессии, добавление упражнения, завершение) она
    # уже срабатывает.
    workout_session = session_exercise.workout_session
    phase_effort_tier = await progression_repo.resolve_phase_effort_tier(
        db, workout_session.app_user_mesocycle_id, workout_session.mesocycle_phase,
        training_block_id=workout_session.training_block_id,
    )

    data = await compute_autoprogression(
        db,
        session_exercise,
        current_app_user.id,
        experience_level,
        settings,
        target_reps=target_reps,
        target_effort=target_effort,
        phase_effort_tier=phase_effort_tier,
        load_context=load_context,
    )

    return AutoprogressionResponse(**data)


async def get_active_workout_for_user(
    db: AsyncSession,
    app_user_id: int,
) -> WorkoutSession | None:
    stmt = (
        select(WorkoutSession)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.status == "active",
        )
        .options(
            _workout_session_options()
        )
        .order_by(WorkoutSession.started_at.desc())
    )

    result = await db.execute(stmt)
    return result.scalars().first()

@router.patch(
    "/workout-session-sets/{set_id}",
    response_model=AddWorkoutSetResponse,
)
async def update_workout_session_set(
    set_id: int,
    payload: UpdateWorkoutSetRequest,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSessionSet)
        .where(WorkoutSessionSet.id == set_id)
        .options(
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await db.execute(stmt)
    workout_set = result.scalar_one_or_none()

    if workout_set is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    session_exercise = workout_set.workout_session_exercise
    workout = session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    before_analytics = await fingerprint(db, workout.id)
    previous_review = deepcopy(workout_set.anomaly_review)
    apply_set_context(workout_set, payload, session_exercise)
    update_data = payload.model_dump(exclude_unset=True)
    update_data.pop("analytics_load_snapshot", None)
    update_data.pop("anomaly_review", None)

    if "warmup_snapshot" in payload.model_fields_set:
        update_data["warmup_snapshot"] = snapshot_json(payload.warmup_snapshot)
        if payload.warmup_snapshot is not None:
            try:
                validate_warmup_rows(session_exercise.warmup_group,
                    [s.warmup_snapshot for s in session_exercise.sets if s.id != set_id]
                    + [payload.warmup_snapshot])
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    # anomaly_confirmed — флаг запроса, а не колонка модели. Если оставить его
    # в update_data, цикл setattr ниже повесит посторонний атрибут на ORM-объект.
    anomaly_confirmed = bool(update_data.pop("anomaly_confirmed", False))

    if CONTEXT_KEYS.intersection(update_data):
        mode = update_data.get("load_mode", workout_set.load_mode)
        gym_id = update_data.get("gym_profile_id", workout_set.gym_profile_id)
        setup_id = update_data.get("setup_id", workout_set.setup_id)
        if "load_mode" in update_data and mode != workout_set.load_mode and "setup_id" not in update_data:
            setup_id = None
        if "gym_profile_id" in update_data and gym_id != workout_set.gym_profile_id and "setup_id" not in update_data:
            setup_id = None
        try:
            context = await resolve_set_context(
                db, current_app_user.id, session_exercise, mode=mode, gym_id=gym_id,
                setup_id=setup_id, supplied_snapshot=update_data.get("load_snapshot"),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        for key, value in context.items():
            update_data[key] = value
    if "shown_target_snapshot" in update_data:
        update_data["shown_target_snapshot"] = deepcopy(update_data["shown_target_snapshot"])

    if "parent_set_id" in update_data and update_data["parent_set_id"] is not None:
        parent_exists = any(s.id == update_data["parent_set_id"] for s in session_exercise.sets)
        if not parent_exists:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent set does not belong to this exercise",
            )

    for field_name, value in update_data.items():
        setattr(workout_set, field_name, value)

    if ({"weight", "reps", "set_type", "analytics_load_snapshot", "anomaly_review"} & payload.model_fields_set
            and before_analytics != await fingerprint(db, workout.id)) or "anomaly_confirmed" in payload.model_fields_set:
        await review_set(db, workout, session_exercise, workout_set, previous_review=previous_review, confirmed=anomaly_confirmed if 'anomaly_confirmed' in payload.model_fields_set else None)

    if before_analytics != await fingerprint(db, workout.id):
        await mark_dirty(db, current_app_user.id)
    await db.commit()
    await db.refresh(workout_set)

    return workout_set

@router.post(
    "/workout-session-sets/{set_id}/repeat",
    response_model=AddWorkoutSetResponse,
)
async def repeat_workout_session_set(
    set_id: int,
    payload: RepeatWorkoutSetRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSessionSet)
        .where(WorkoutSessionSet.id == set_id)
        .options(
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await db.execute(stmt)
    source_set = result.scalar_one_or_none()

    if source_set is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    source_session_exercise = source_set.workout_session_exercise
    workout = source_session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    target_session_exercise = source_session_exercise

    if payload is not None and payload.target_session_exercise_id is not None:
        target_stmt = (
            select(WorkoutSessionExercise)
            .where(WorkoutSessionExercise.id == payload.target_session_exercise_id)
            .options(
                selectinload(WorkoutSessionExercise.workout_session),
                selectinload(WorkoutSessionExercise.sets),
            )
        )
        target_result = await db.execute(target_stmt)
        target_session_exercise = target_result.scalar_one_or_none()

        if target_session_exercise is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target workout session exercise not found",
            )

        if target_session_exercise.workout_session.app_user_id != current_app_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target workout session exercise not found",
            )

        if target_session_exercise.workout_session.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workout is not active",
            )

        if (target_session_exercise.exercise_id != source_session_exercise.exercise_id
                and (source_set.load_mode is not None or source_set.setup_id is not None
                     or source_set.analytics_load_snapshot is not None)):
            raise HTTPException(status_code=400,
                                detail="Machine context cannot be repeated for a different exercise")

    next_set_number = len(target_session_exercise.sets) + 1

    repeated_snapshot = None
    if source_set.warmup_snapshot and target_session_exercise.warmup_group:
        group_id = target_session_exercise.warmup_group["id"]
        orders = [s.warmup_snapshot["order"] for s in target_session_exercise.sets
                  if s.warmup_snapshot and s.warmup_snapshot["group_id"] == group_id]
        repeated_snapshot = {"group_id": group_id, "calculation_version": None,
            "order": max(orders, default=0) + 1, "fraction": None,
            "raw_weight_kg": None, "manually_edited": True}

    repeated_set = WorkoutSessionSet(
        workout_session_exercise_id=target_session_exercise.id,
        set_number=next_set_number,
        set_type=source_set.set_type,
        weight=source_set.weight,
        reps=source_set.reps,
        effort_level=source_set.effort_level,
        notes=source_set.notes,
        parent_set_id=None,
        superset_round=source_set.superset_round,
        is_completed=True,
        # Повтор наследует пометку исходного подхода: пересчитывать вердикт
        # заново нельзя — это стёрло бы подтверждение, которое пользователь
        # уже дал по оригиналу.
        is_anomalous=source_set.is_anomalous,
        # P1-14: режим «на максимум повторов» наследуется так же — повтор
        # подхода-максимума должен остаться подходом-максимумом.
        is_max_reps=source_set.is_max_reps,
        load_mode=source_set.load_mode,
        gym_profile_id=source_set.gym_profile_id,
        setup_id=source_set.setup_id,
        load_snapshot=deepcopy(source_set.load_snapshot),
        shown_target_snapshot=deepcopy(source_set.shown_target_snapshot),
        warmup_snapshot=repeated_snapshot,
    )

    if analytics_enabled() or source_set.analytics_load_snapshot is not None:
        repeated_set.client_uuid = str(uuid4())
    repeated_set.analytics_load_snapshot = deepcopy(source_set.analytics_load_snapshot)
    repeated_set.anomaly_review = deepcopy(source_set.anomaly_review)
    db.add(repeated_set)
    await mark_dirty(db, current_app_user.id)
    await db.commit()
    await db.refresh(repeated_set)

    return repeated_set

@router.delete(
    "/workout-session-exercises/{session_exercise_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workout_session_exercise(
    session_exercise_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSessionExercise)
        .where(WorkoutSessionExercise.id == session_exercise_id)
        .options(
            selectinload(WorkoutSessionExercise.workout_session)
            .selectinload(WorkoutSession.exercises)
        )
    )

    result = await db.execute(stmt)
    session_exercise = result.scalar_one_or_none()

    if session_exercise is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    workout = session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout session exercise not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    deleted_exercise_id = session_exercise.id

    await record_child_deletion(db, workout, "workout_exercise", session_exercise.client_uuid, session_exercise.id)
    await db.delete(session_exercise)
    await mark_dirty(db, current_app_user.id)
    await db.flush()

    remaining_exercises = sorted(
        [e for e in workout.exercises if e.id != deleted_exercise_id],
        key=lambda e: e.order_index,
    )

    for index, exercise in enumerate(remaining_exercises):
        exercise.order_index = index

    await db.commit()

@router.delete(
    "/workout-session-sets/{set_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_workout_session_set(
    set_id: int,
    db: AsyncSession = Depends(get_db),
    current_app_user=Depends(get_current_app_user),
):
    stmt = (
        select(WorkoutSessionSet)
        .where(WorkoutSessionSet.id == set_id)
        .options(
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.workout_session),
            selectinload(WorkoutSessionSet.workout_session_exercise)
            .selectinload(WorkoutSessionExercise.sets),
        )
    )

    result = await db.execute(stmt)
    workout_set = result.scalar_one_or_none()

    if workout_set is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    session_exercise = workout_set.workout_session_exercise
    workout = session_exercise.workout_session

    if workout.app_user_id != current_app_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workout set not found",
        )

    if workout.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workout is not active",
        )

    deleted_set_id = workout_set.id

    await record_child_deletion(db, workout, "workout_set", workout_set.client_uuid, workout_set.id)
    await db.delete(workout_set)
    await db.flush()

    session_exercise.sets = [
        s for s in session_exercise.sets if s.id != deleted_set_id
    ]
    _renumber_sets(session_exercise)
    await mark_dirty(db, current_app_user.id)

    await db.commit()

@router.post("/workouts/{workout_id}/supersets")
async def create_superset(
    workout_id: int,
    payload: CreateSupersetRequest,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    superset_group = await WorkoutSupersetService.create_superset(
        session=session,
        workout_id=workout_id,
        app_user_id=app_user.id,
        source_session_exercise_id=payload.source_session_exercise_id,
        target_session_exercise_ids=payload.target_session_exercise_ids,
    )

    return {
        "superset_group": superset_group,
    }

@router.patch("/workouts/{workout_id}/structure")
async def reorder_workout_structure(
    workout_id: int,
    payload: ReorderWorkoutStructureRequest,
    session: AsyncSession = Depends(get_db),
    app_user: AppUser = Depends(get_current_app_user),
):
    await WorkoutSupersetService.reorder_workout_structure(
        session=session,
        workout_id=workout_id,
        app_user_id=app_user.id,
        items=payload.items,
    )
    return {"ok": True}


@router.patch("/workouts/{workout_id}/analytics-context", response_model=WorkoutSessionDetailResponse)
async def update_analytics_context(workout_id: int, payload: UpdateAnalyticsContextRequest,
                                   db: AsyncSession = Depends(get_db),
                                   current_app_user=Depends(get_current_app_user)):
    workout = (await db.execute(select(WorkoutSession).where(
        WorkoutSession.id == workout_id, WorkoutSession.app_user_id == current_app_user.id)
        .options(_workout_session_options()).with_for_update())).scalar_one_or_none()
    if workout is None:
        raise HTTPException(404, 'Workout not found')
    if workout.status != 'active':
        raise HTTPException(409, {'error': 'history_revision_required'})
    if payload.base_sync_version != (workout.sync_version or 0):
        raise HTTPException(409, {'error': 'sync_conflict', 'sync_version': workout.sync_version or 0,
            'workout': WorkoutSessionDetailResponse.model_validate(workout).model_dump(mode='json')})
    before = await fingerprint(db, workout.id)
    if not analytics_enabled():
        raise HTTPException(409, {'error': 'analytics_disabled'})
    if 'explicit_mass' in payload.model_fields_set:
        if payload.explicit_mass is None:
            old = deepcopy(workout.analytics_snapshot) or {'schema_version': 1}
            old['body_mass'] = None
            workout.analytics_snapshot = old
        else:
            await capture_workout_snapshot(db, workout, explicit_mass=payload.explicit_mass)
    if payload.analytics_mass_intent is not None:
        from api.services.analytics.writes import apply_session_context
        await apply_session_context(db, workout, payload)
    by_id = {exercise.id: exercise for exercise in workout.exercises}
    for exercise_id, defaults in (payload.load_defaults or {}).items():
        if exercise_id not in by_id:
            raise HTTPException(404, 'Workout exercise not found')
        by_id[exercise_id].analytics_load_defaults = json_value(defaults)
    await review_workout(db, workout)
    if before != await fingerprint(db, workout.id):
        workout.sync_version = (workout.sync_version or 0) + 1
        await mark_dirty(db, current_app_user.id)
    await db.commit()
    return (await db.execute(select(WorkoutSession).where(WorkoutSession.id == workout_id)
        .options(_workout_session_options()).execution_options(populate_existing=True))).scalar_one()
