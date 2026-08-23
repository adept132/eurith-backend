"""Резолв упражнений и запись импортированной истории в БД.

Парсинг файла живёт в csv_import (чистый модуль). Здесь — всё, что требует БД:
каскад сопоставления имён, дедупликация и вставка сессий.
"""

import unicodedata
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.csv_format import wall_clock_to_utc
from api.services.csv_import import ParsedWorkout
from api.services.exercise_matcher import ExerciseMatcher
from api.services.exercise_utils import get_base_exercise_query
from api.services.models import (
    Exercise,
    ExerciseImportAlias,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.strong_dictionary import lookup_ru_name, split_equipment

IMPORT_SOURCE_STRONG = "strong"
IMPORT_SOURCES = ("strong", "hevy", "fitbod", "table", "notes")

# Порог, ниже которого fuzzy-совпадению не доверяем и спрашиваем пользователя.
# Намеренно строгий: кривой маппинг отравляет бюджет объёма и DUP.
MIN_AUTO_MATCH_SIMILARITY = 0.75

# Порог показа подсказок на экране сопоставления. Для англоязычных имён
# кросс-языковой fuzzy выдаёт шум («Snatch» -> «Bayesian curls», 0.30):
# пустой список честнее мусорной подсказки.
MIN_SUGGESTION_SIMILARITY = 0.45


def normalize_external_name(external_name: str) -> str:
    """Build the stable identity used by alias save and lookup.

    NFKC folds compatibility forms (for example full-width Latin), split/join
    trims and collapses every Unicode whitespace run, and casefold provides
    Unicode caseless matching rather than ASCII-oriented ``lower``.
    """
    if not isinstance(external_name, str):
        raise TypeError("external_name must be a string")
    compatible = unicodedata.normalize("NFKC", external_name)
    collapsed = " ".join(compatible.split())
    return unicodedata.normalize("NFKC", collapsed.casefold())


def _validated_source(source: str) -> str:
    if source not in IMPORT_SOURCES:
        raise ValueError(f"unsupported import source: {source}")
    return source


class ExerciseResolver:
    """Каскад: Exercise ID -> алиас -> словарь EN->RU -> fuzzy -> не найдено."""

    def __init__(
        self,
        session: AsyncSession,
        app_user_id: int,
        source: str = IMPORT_SOURCE_STRONG,
    ):
        self.session = session
        self.app_user_id = app_user_id
        self.source = _validated_source(source)
        self._by_id: Dict[int, Exercise] = {}
        self._by_name: Dict[str, Exercise] = {}
        self._aliases: Dict[str, int] = {}
        self._cache: Dict[str, Optional[int]] = {}

    async def load(self) -> None:
        self._by_id.clear()
        self._by_name.clear()
        self._aliases.clear()
        self._cache.clear()
        result = await self.session.execute(get_base_exercise_query(self.app_user_id))
        for ex in result.scalars().all():
            self._by_id[ex.id] = ex
            self._by_name[normalize_external_name(ex.name)] = ex

        alias_rows = await self.session.execute(
            select(ExerciseImportAlias).where(
                ExerciseImportAlias.app_user_id == self.app_user_id,
                ExerciseImportAlias.source == self.source,
            )
        )
        for a in alias_rows.scalars().all():
            self._aliases[a.normalized_external_name] = a.exercise_id

    async def resolve(
        self, name: str, exercise_id: Optional[int] = None
    ) -> Optional[int]:
        """Возвращает id нашего упражнения или None, если сопоставить не смогли."""
        # 1. Точный id из нашего же экспорта.
        if exercise_id and exercise_id in self._by_id:
            return exercise_id

        key = normalize_external_name(name)
        if key in self._cache:
            return self._cache[key]

        resolved = await self._resolve_uncached(name, key)
        self._cache[key] = resolved
        return resolved

    async def _resolve_uncached(self, name: str, key: str) -> Optional[int]:
        # 2. Ранее сохранённый ручной выбор пользователя.
        alias_id = self._aliases.get(key)
        if alias_id and alias_id in self._by_id:
            return alias_id

        # 3. Точное совпадение по имени (наш экспорт с русскими именами).
        exact = self._by_name.get(key)
        if exact:
            return exact.id

        # 4. Словарь EN->RU для англоязычных имён Strong.
        ru_name = lookup_ru_name(name) if self.source == IMPORT_SOURCE_STRONG else None
        if ru_name:
            hit = self._by_name.get(normalize_external_name(ru_name))
            if hit:
                return hit.id

        # 5. Fuzzy — сработает для русских имён с опечатками/вариациями.
        #    Для англоязычных имён это почти всегда мимо, поэтому порог высокий.
        base_name = split_equipment(name)[0]
        best, _ = await ExerciseMatcher.find_or_create_exercise(
            self.session, self.app_user_id, base_name
        )
        if best and best.get("similarity", 0) >= MIN_AUTO_MATCH_SIMILARITY:
            return best["id"]

        return None

    async def suggestions(self, name: str, limit: int = 5) -> List[Dict]:
        """Кандидаты для экрана ручного сопоставления."""
        base_name = split_equipment(name)[0]
        _, candidates = await ExerciseMatcher.find_or_create_exercise(
            self.session, self.app_user_id, base_name
        )
        return [
            {
                "id": c["id"],
                "name": c["name"],
                "main_muscle_group": c.get("main_muscle_group"),
                "similarity": round(c.get("similarity", 0), 3),
            }
            for c in candidates
            if c.get("similarity", 0) >= MIN_SUGGESTION_SIMILARITY
        ][:limit]


async def existing_import_keys(
    session: AsyncSession, app_user_id: int, keys: List[str]
) -> set:
    """Какие из ключей уже импортированы (для дедупа)."""
    if not keys:
        return set()
    result = await session.execute(
        select(WorkoutSession.import_key).where(
            WorkoutSession.app_user_id == app_user_id,
            WorkoutSession.import_key.in_(keys),
        )
    )
    return {row[0] for row in result.all()}


def build_alias_upsert(
    *,
    app_user_id: int,
    source: str,
    external_name: str,
    exercise_id: int,
):
    """Compile one PostgreSQL insert-or-update for an alias identity."""
    source = _validated_source(source)
    normalized_name = normalize_external_name(external_name)
    if not normalized_name:
        raise ValueError("external_name must contain non-whitespace text")

    statement = insert(ExerciseImportAlias).values(
        app_user_id=app_user_id,
        source=source,
        external_name=external_name,
        normalized_external_name=normalized_name,
        exercise_id=exercise_id,
    )
    return (
        statement.on_conflict_do_update(
            constraint="uq_exercise_import_alias_user_source_name",
            set_={
                "external_name": statement.excluded.external_name,
                "exercise_id": statement.excluded.exercise_id,
            },
        )
        .returning(ExerciseImportAlias)
    )


async def save_alias(
    session: AsyncSession,
    app_user_id: int,
    external_name: str,
    exercise_id: int,
    source: str = IMPORT_SOURCE_STRONG,
) -> ExerciseImportAlias:
    """Persist a source-aware alias without a select-then-insert race."""
    result = await session.execute(
        build_alias_upsert(
            app_user_id=app_user_id,
            source=source,
            external_name=external_name,
            exercise_id=exercise_id,
        )
    )
    return result.scalar_one()


async def import_workouts(
    session: AsyncSession,
    app_user_id: int,
    workouts: List[ParsedWorkout],
    resolver: ExerciseResolver,
    skip_keys: set,
    tz_name: Optional[str] = None,
) -> Tuple[int, int, int]:
    """Пишет тренировки в БД. Возвращает (добавлено, пропущено_дублей, пропущено_упражнений).

    Тренировки с source='free': значение 'import' нарушило бы CheckConstraint
    на workout_sessions.source (init_db не обновляет констрейнты). Провенанс
    держим в import_source/import_key.

    tz_name — часовой пояс пользователя: дата в файле это настенное время без
    смещения, и переводить её в UTC нужно явно, иначе наивную дату
    проинтерпретирует таймзона соединения с БД и время уедет.
    """
    added = 0
    duplicates = 0
    unresolved_exercises = 0

    for w in workouts:
        if w.import_key in skip_keys:
            duplicates += 1
            continue

        started_at = wall_clock_to_utc(w.started_at, tz_name)
        finished_at = None
        if w.duration_seconds:
            finished_at = started_at + timedelta(seconds=w.duration_seconds)

        ws = WorkoutSession(
            app_user_id=app_user_id,
            source="free",
            status="finished",
            started_at=started_at,
            finished_at=finished_at,
            notes=w.notes,
            import_source=IMPORT_SOURCE_STRONG,
            import_key=w.import_key,
        )
        session.add(ws)
        await session.flush()

        order_index = 0
        wrote_any = False

        for parsed_ex in w.exercises:
            resolved_id = await resolver.resolve(parsed_ex.name, parsed_ex.exercise_id)
            if not resolved_id:
                unresolved_exercises += 1
                continue

            order_index += 1
            ws_ex = WorkoutSessionExercise(
                workout_session_id=ws.id,
                exercise_id=resolved_id,
                order_index=order_index,
                superset_group=parsed_ex.superset_group,
            )
            session.add(ws_ex)
            await session.flush()

            # Сначала корневые подходы: дропсету нужен id родителя, который
            # в файле выражен через Parent Set Order.
            id_by_set_order: Dict[int, int] = {}
            ordered = sorted(parsed_ex.sets, key=lambda s: (s.parent_set_order is not None, s.set_order))

            for ps in ordered:
                new_set = WorkoutSessionSet(
                    workout_session_exercise_id=ws_ex.id,
                    set_number=ps.set_order,
                    set_type=ps.set_type,
                    weight=ps.weight_kg,
                    reps=ps.reps,
                    notes=ps.notes,
                    effort_level=ps.effort_level,
                    is_completed=ps.is_completed,
                    parent_set_id=id_by_set_order.get(ps.parent_set_order)
                    if ps.parent_set_order
                    else None,
                    superset_round=ps.superset_round,
                )
                session.add(new_set)
                await session.flush()
                id_by_set_order.setdefault(ps.set_order, new_set.id)
                wrote_any = True

        if wrote_any:
            added += 1
        else:
            # Ни одно упражнение не легло — пустую сессию не оставляем.
            await session.delete(ws)

    return added, duplicates, unresolved_exercises
