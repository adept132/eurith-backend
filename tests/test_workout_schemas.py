"""P0-09: ExerciseShortResponse отдаёт нормализованные системные ключи мышц.

Раньше клиент нормализовал русские названия сам, тремя расходящимися копиями
RU_TO_EN_MAP. Нормализация переехала на бэкенд, в единственный to_system_key —
эта схема обязана заполнять muscle_key/secondary_muscle_keys независимо от
того, как именно main_muscle_group/secondary_muscle_groups хранятся в БД.
"""

from api.schemas.workouts import ExerciseShortResponse
from api.schemas.sync import SyncExerciseSnapshot, SyncSetSnapshot, SyncWorkoutSnapshot
from datetime import datetime, timezone


def test_sync_set_distinguishes_omitted_context_from_explicit_clear():
    old = SyncSetSnapshot(client_uuid="set-1", set_number=1)
    edited = SyncSetSnapshot(client_uuid="set-1", set_number=1, load_mode=None,
                             shown_target_snapshot=None)
    assert "load_mode" not in old.model_fields_set
    assert "shown_target_snapshot" not in old.model_fields_set
    assert {"load_mode", "shown_target_snapshot"} <= edited.model_fields_set


def test_sync_session_and_exercise_distinguish_missing_from_clear():
    base = dict(client_uuid="workout-1", source="free", status="active",
                started_at=datetime.now(timezone.utc))
    old = SyncWorkoutSnapshot(**base)
    cleared = SyncWorkoutSnapshot(**base, gym_profile_id=None, gym_snapshot=None)
    assert "gym_profile_id" not in old.model_fields_set
    assert {"gym_profile_id", "gym_snapshot"} <= cleared.model_fields_set
    old_ex = SyncExerciseSnapshot(client_uuid="exercise-1", exercise_id=1, order_index=0)
    cleared_ex = SyncExerciseSnapshot(client_uuid="exercise-1", exercise_id=1,
                                      order_index=0, active_load_mode=None,
                                      active_setup_id=None)
    assert "active_load_mode" not in old_ex.model_fields_set
    assert {"active_load_mode", "active_setup_id"} <= cleared_ex.model_fields_set


def test_muscle_key_normalized_from_russian_main_group():
    resp = ExerciseShortResponse(
        id=1, name="Жим лёжа", main_muscle_group="Грудь",
    )
    assert resp.muscle_key == "chest"


def test_secondary_muscle_keys_normalized_from_list():
    resp = ExerciseShortResponse(
        id=1, name="Жим лёжа", main_muscle_group="Грудь",
        secondary_muscle_groups=["Трицепс", "Передняя дельта"],
    )
    assert resp.secondary_muscle_keys == ["triceps", "front_delts"]


def test_secondary_muscle_keys_survive_string_shape():
    # secondary_muscle_groups иногда приходит строкой, а не списком.
    resp = ExerciseShortResponse(
        id=1, name="Жим лёжа", main_muscle_group="Грудь",
        secondary_muscle_groups="Трицепс, Передняя дельта",
    )
    assert resp.secondary_muscle_keys == ["triceps", "front_delts"]


def test_unknown_muscle_names_drop_to_none_and_are_filtered():
    resp = ExerciseShortResponse(
        id=1, name="Загадочное упражнение", main_muscle_group="Неведома зверушка",
        secondary_muscle_groups=["Тоже неизвестно", "Трицепс"],
    )
    assert resp.muscle_key is None
    assert resp.secondary_muscle_keys == ["triceps"]


def test_missing_main_muscle_group_yields_none_key_and_empty_secondary():
    resp = ExerciseShortResponse(id=1, name="Без мышцы")
    assert resp.muscle_key is None
    assert resp.secondary_muscle_keys == []


def test_already_system_key_passes_through():
    # main_muscle_group у кастомных упражнений иногда уже системный ключ.
    resp = ExerciseShortResponse(
        id=1, name="Кастом", main_muscle_group="chest",
        secondary_muscle_groups=["triceps"],
    )
    assert resp.muscle_key == "chest"
    assert resp.secondary_muscle_keys == ["triceps"]


def test_secondary_muscle_keys_deduplicated_when_two_ru_synonyms_collapse():
    """P0-09 I5 (Important): to_system_key схлопывает "Трапеция" и
    "Трапеции" (единственное/множественное — оба варианта живут в каталоге,
    см. api/services/muscle_keys.py) на один и тот же ключ "traps". Без
    дедупа список нёс бы этот ключ дважды — клиентский трекер суммирует
    += по каждому элементу и удвоил бы вклад мышцы, хотя measure.contribution()
    на сервере такого дубля не считает вовсе."""
    resp = ExerciseShortResponse(
        id=1, name="Шраги", main_muscle_group="Грудь",
        secondary_muscle_groups=["Трапеция", "Трапеции"],
    )
    assert resp.secondary_muscle_keys == ["traps"]


def test_secondary_muscle_keys_exclude_primary_key():
    # Если каталог продублировал главную мышцу среди синергистов, прямой
    # вклад уже учтён через muscle_key — второй раз его в secondary не несём.
    resp = ExerciseShortResponse(
        id=1, name="Жим лёжа", main_muscle_group="Грудь",
        secondary_muscle_groups=["Грудь", "Трицепс"],
    )
    assert resp.muscle_key == "chest"
    assert resp.secondary_muscle_keys == ["triceps"]
