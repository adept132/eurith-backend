"""Резолв упражнений и запись импортированной истории в БД.

Парсинг файла живёт в csv_import (чистый модуль). Здесь — всё, что требует БД:
каскад сопоставления имён, дедупликация и вставка сессий.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, List, Mapping, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from api.services.csv_format import wall_clock_to_utc
from api.services.csv_import import ParsedWorkout
from api.services.exercise_matcher import ExerciseMatcher
from api.services.exercise_alias_identity import (
    normalize_external_name,
    normalized_external_name_sha256_from_normalized,
    validate_exercise_id,
    validate_external_name,
)
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


def _validated_source(source: str) -> str:
    if source not in IMPORT_SOURCES:
        raise ValueError(f"unsupported import source: {source}")
    return source


class ExerciseAliasNotAccessibleError(LookupError):
    """The target exercise is unknown or private to another user."""


class ExerciseAliasIdentityCollisionError(RuntimeError):
    """A fixed-size identity key matched a different full normalized name."""


@dataclass(frozen=True)
class _PreparedAlias:
    source: str
    external_name: str
    normalized_external_name: str
    normalized_external_name_sha256: str
    exercise_id: int


def _prepare_alias(
    *,
    source: str,
    external_name: str,
    exercise_id: int,
) -> _PreparedAlias:
    validated_source = _validated_source(source)
    normalized_name = validate_external_name(external_name)
    validated_exercise_id = validate_exercise_id(exercise_id)
    return _PreparedAlias(
        source=validated_source,
        external_name=external_name,
        normalized_external_name=normalized_name,
        normalized_external_name_sha256=(
            normalized_external_name_sha256_from_normalized(normalized_name)
        ),
        exercise_id=validated_exercise_id,
    )


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
    prepared = _prepare_alias(
        source=source,
        external_name=external_name,
        exercise_id=exercise_id,
    )
    return _build_alias_upsert(app_user_id=app_user_id, aliases=[prepared])


def _build_alias_upsert(*, app_user_id: int, aliases: List[_PreparedAlias]):
    statement = insert(ExerciseImportAlias).values(
        [
            {
                "app_user_id": app_user_id,
                "source": alias.source,
                "external_name": alias.external_name,
                "normalized_external_name": alias.normalized_external_name,
                "normalized_external_name_sha256": (
                    alias.normalized_external_name_sha256
                ),
                "exercise_id": alias.exercise_id,
            }
            for alias in aliases
        ]
    )
    return (
        statement.on_conflict_do_update(
            constraint="uq_exercise_import_alias_user_source_name",
            set_={
                "external_name": statement.excluded.external_name,
                "exercise_id": statement.excluded.exercise_id,
            },
            # A digest collision must never overwrite an unrelated alias. A
            # false predicate returns no row, which callers turn into a safe
            # explicit failure instead of corrupting the existing identity.
            where=(
                ExerciseImportAlias.normalized_external_name
                == statement.excluded.normalized_external_name
            ),
        )
        .returning(ExerciseImportAlias)
    )


def _locked_accessible_exercise_query(app_user_id: int, exercise_ids: List[int]):
    return (
        get_base_exercise_query(app_user_id)
        .where(Exercise.id.in_(exercise_ids))
        .order_by(Exercise.id)
        # FOR SHARE keeps both the exercise row and its ownership stable until
        # the alias FK is written; unknown/foreign rows are never locked.
        .with_for_update(read=True, of=Exercise)
    )


async def save_alias(
    session: AsyncSession,
    app_user_id: int,
    external_name: str,
    exercise_id: int,
    source: str = IMPORT_SOURCE_STRONG,
) -> ExerciseImportAlias:
    """Validate ownership and atomically persist one source-aware alias."""
    prepared = _prepare_alias(
        source=source,
        external_name=external_name,
        exercise_id=exercise_id,
    )
    exercise = (
        await session.execute(
            _locked_accessible_exercise_query(app_user_id, [prepared.exercise_id])
        )
    ).scalar_one_or_none()
    if exercise is None:
        raise ExerciseAliasNotAccessibleError("exercise not found")

    result = await session.execute(
        _build_alias_upsert(app_user_id=app_user_id, aliases=[prepared]),
        execution_options={"populate_existing": True},
    )
    try:
        alias = result.scalar_one()
    except NoResultFound as exc:
        raise ExerciseAliasIdentityCollisionError(
            "alias identity digest collision"
        ) from exc

    # The locked Exercise came from the same session. Marking it committed
    # keeps the service's return type backwards-compatible while letting the
    # API serialize the validated exercise without a second ownership query.
    set_committed_value(alias, "exercise", exercise)
    return alias


async def save_aliases(
    session: AsyncSession,
    app_user_id: int,
    mappings: Mapping[str, int],
    source: str = IMPORT_SOURCE_STRONG,
    *,
    skip_inaccessible: bool = False,
    skip_invalid: bool = False,
) -> List[ExerciseImportAlias]:
    """Validate and persist a mapping batch with one ownership read and write.

    ``skip_*`` is used by the legacy import endpoint to preserve its response
    contract: malformed, unknown, and foreign-private targets are all ignored
    and only successfully persisted aliases contribute to ``saved_aliases``.
    """
    validated_source = _validated_source(source)
    normalized_by_digest: Dict[str, str] = {}
    prepared_aliases: List[_PreparedAlias] = []
    for external_name, exercise_id in mappings.items():
        try:
            prepared = _prepare_alias(
                source=validated_source,
                external_name=external_name,
                exercise_id=exercise_id,
            )
        except (TypeError, ValueError):
            if skip_invalid:
                continue
            raise
        previous_name = normalized_by_digest.setdefault(
            prepared.normalized_external_name_sha256,
            prepared.normalized_external_name,
        )
        if previous_name != prepared.normalized_external_name:
            raise ExerciseAliasIdentityCollisionError(
                "alias identity digest collision"
            )
        prepared_aliases.append(prepared)

    if not prepared_aliases:
        return []

    exercise_ids = sorted({alias.exercise_id for alias in prepared_aliases})
    exercises = (
        await session.execute(
            _locked_accessible_exercise_query(app_user_id, exercise_ids)
        )
    ).scalars().all()
    exercise_by_id = {exercise.id: exercise for exercise in exercises}
    inaccessible_ids = set(exercise_ids) - exercise_by_id.keys()
    if inaccessible_ids and not skip_inaccessible:
        raise ExerciseAliasNotAccessibleError("exercise not found")

    accessible_attempts = [
        alias for alias in prepared_aliases if alias.exercise_id in exercise_by_id
    ]
    if not accessible_attempts:
        return []

    # Filter inaccessible targets before deduplicating. Otherwise a later
    # foreign-private target with an equivalent spelling could suppress an
    # earlier valid mapping. Among accessible choices, the latest wins just as
    # it did when the legacy writer executed its upserts in mapping order.
    accessible_by_identity: Dict[Tuple[str, str], _PreparedAlias] = {}
    for alias in accessible_attempts:
        accessible_by_identity[
            (alias.source, alias.normalized_external_name)
        ] = alias
    accessible_aliases = list(accessible_by_identity.values())

    result = await session.execute(
        _build_alias_upsert(app_user_id=app_user_id, aliases=accessible_aliases),
        execution_options={"populate_existing": True},
    )
    saved_aliases = result.scalars().all()
    if len(saved_aliases) != len(accessible_aliases):
        raise ExerciseAliasIdentityCollisionError(
            "alias identity digest collision"
        )
    for alias in saved_aliases:
        set_committed_value(alias, "exercise", exercise_by_id[alias.exercise_id])
    saved_by_identity = {
        (alias.source, alias.normalized_external_name): alias
        for alias in saved_aliases
    }
    # Repeat the returned identity for equivalent accessible input spellings so
    # the legacy ``saved_aliases`` count keeps its prior per-mapping semantics.
    return [
        saved_by_identity[(alias.source, alias.normalized_external_name)]
        for alias in accessible_attempts
    ]


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
