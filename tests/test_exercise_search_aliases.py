from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api.routers import exercises as exercises_router
from api.schemas.exercises import ExerciseSearchItem
from api.services import strong_dictionary
from api.services.exercise_search_service import ExerciseSearchService
from api.services.models import Exercise


BENCH_PRESS_RU = "Жим лежа на прямой скамье"
DUMBBELL_BENCH_PRESS_RU = "Жим гантелей лежа на горизонтальной скамье"

GENERIC_EQUIPMENT_COLLISIONS = [
    (
        "Тяга штанги в наклоне",
        "Barbell Bent Over Row",
        "Bent Over Row",
    ),
    (
        "Разведение рук в стороны с гантелями стоя",
        "Dumbbell Lateral Raise",
        "Lateral Raise",
    ),
    (
        "Обратная бабочка",
        "Machine Reverse Fly",
        "Reverse Fly",
    ),
]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _QueuedSession:
    def __init__(self, *results):
        self._results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self._results.pop(0))


def _exercise(exercise_id: int, name: str) -> Exercise:
    return Exercise(
        id=exercise_id,
        name=name,
        category="base",
        main_muscle_group="Грудь",
        secondary_muscle_groups=[],
        equipment_needed=["barbell"],
        difficulty="intermediate",
        fatigue_tier=2,
        source="default",
        image_urls=[],
        image_approx=False,
    )


def _search_dict(exercise_id: int, name: str) -> dict:
    return {
        "id": exercise_id,
        "name": name,
        "category": "base",
        "main_muscle_group": "Грудь",
        "secondary_muscle_groups": [],
        "equipment_needed": ["barbell"],
        "fatigue_tier": 2,
        "source": "default",
        "image_urls": [],
        "image_approx": False,
    }


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "scheme": "https",
            "server": ("testserver", 443),
            "path": "/search",
            "headers": [],
        }
    )


def test_search_schema_uses_a_fresh_empty_alias_list() -> None:
    first = ExerciseSearchItem(
        id=1,
        name="Первое",
        main_muscle_group="Грудь",
        category="base",
        source="default",
    )
    second = ExerciseSearchItem(
        id=2,
        name="Второе",
        main_muscle_group="Спина",
        category="base",
        source="default",
    )

    first.aliases.append("Private alias")

    assert first.aliases == ["Private alias"]
    assert second.aliases == []


def test_reverse_strong_aliases_are_equipment_aware_and_exclude_bare_collisions() -> None:
    aliases_by_name = strong_dictionary.build_reverse_strong_aliases()

    assert "Barbell Bench Press" in aliases_by_name[BENCH_PRESS_RU]
    assert "Dumbbell Bench Press" in aliases_by_name[DUMBBELL_BENCH_PRESS_RU]
    assert all(
        "Bench Press" not in aliases
        for aliases in aliases_by_name.values()
    )


@pytest.mark.parametrize(
    ("russian_name", "qualified_alias", "bare_alias"),
    GENERIC_EQUIPMENT_COLLISIONS,
)
def test_generic_equipment_collisions_keep_the_safe_qualified_alias(
    russian_name: str,
    qualified_alias: str,
    bare_alias: str,
) -> None:
    aliases_by_name = strong_dictionary.build_reverse_strong_aliases()

    assert qualified_alias in aliases_by_name[russian_name]
    assert all(bare_alias not in aliases for aliases in aliases_by_name.values())


def test_qualified_strong_aliases_have_no_cross_exercise_collisions() -> None:
    aliases_by_name = strong_dictionary.build_reverse_strong_aliases()
    targets_by_alias: dict[str, set[str]] = {}
    for russian_name, aliases in aliases_by_name.items():
        for alias in aliases:
            targets_by_alias.setdefault(alias.casefold(), set()).add(russian_name)

    collisions = {
        alias: targets
        for alias, targets in targets_by_alias.items()
        if len(targets) > 1
    }

    assert collisions == {}


@pytest.mark.asyncio
async def test_full_catalog_bulk_attaches_canonical_and_current_user_aliases() -> None:
    bench = _exercise(1, BENCH_PRESS_RU)
    unknown = _exercise(2, "Упражнение без алиасов")
    session = _QueuedSession(
        [bench, unknown],
        [],
        [
            (1, "my bench"),
            (1, "barbell bench press"),
        ],
    )

    items = await ExerciseSearchService.search_exercises(
        session=session,
        user_id=41,
    )

    assert len(session.statements) == 3
    assert bench.name == BENCH_PRESS_RU
    assert bench.aliases == ["Barbell Bench Press", "my bench"]
    assert unknown.aliases == []
    alias_query = session.statements[-1].compile()
    assert 41 in alias_query.params.values()
    assert [1, 2] in alias_query.params.values()


@pytest.mark.asyncio
async def test_text_search_bulk_attaches_aliases_to_matcher_dicts(monkeypatch) -> None:
    match = _search_dict(1, BENCH_PRESS_RU)

    async def fake_matcher(**_kwargs):
        return match, [match]

    monkeypatch.setattr(
        "api.services.exercise_search_service.ExerciseMatcher.find_or_create_exercise",
        fake_matcher,
    )
    session = _QueuedSession([], [(1, "my imported bench")])

    items = await ExerciseSearchService.search_exercises(
        session=session,
        user_id=42,
        q="bench",
    )

    assert len(session.statements) == 2
    assert items[0]["name"] == BENCH_PRESS_RU
    assert items[0]["aliases"] == ["Barbell Bench Press", "my imported bench"]


@pytest.mark.asyncio
async def test_private_alias_query_is_scoped_to_authenticated_user() -> None:
    item = _search_dict(1, BENCH_PRESS_RU)
    session = _QueuedSession([(1, "mine")])

    await ExerciseSearchService.attach_aliases(
        session=session,
        user_id=73,
        items=[item],
    )

    query = session.statements[0].compile()
    sql = str(query)
    assert "exercise_import_aliases.app_user_id" in sql
    assert 73 in query.params.values()
    assert item["aliases"] == ["Barbell Bench Press", "mine"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result_shape", ["dict", "orm"])
async def test_search_router_serializes_aliases_for_both_result_shapes(
    monkeypatch,
    result_shape: str,
) -> None:
    aliases = ["Barbell Bench Press", "my bench"]
    if result_shape == "dict":
        result = _search_dict(1, BENCH_PRESS_RU)
        result["aliases"] = aliases
    else:
        result = _exercise(1, BENCH_PRESS_RU)
        result.aliases = aliases

    async def fake_search(**_kwargs):
        return [result]

    monkeypatch.setattr(
        ExerciseSearchService,
        "search_exercises",
        staticmethod(fake_search),
    )

    response = await exercises_router.search_exercises(
        request=_request(),
        session=object(),
        user=SimpleNamespace(id=9),
    )

    assert response[0].aliases == aliases
