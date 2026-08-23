from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.services.exercise_alias_identity import (
    EXERCISE_ID_MAX,
    EXTERNAL_NAME_MAX_CHARS,
    validate_external_name,
)


ExerciseAliasSource = Literal["strong", "hevy", "fitbod", "table", "notes"]


class StrictExerciseAliasModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExerciseAliasUpsertRequest(StrictExerciseAliasModel):
    source: ExerciseAliasSource
    external_name: str = Field(min_length=1, max_length=EXTERNAL_NAME_MAX_CHARS)
    exercise_id: int = Field(gt=0, le=EXERCISE_ID_MAX)

    @field_validator("external_name")
    @classmethod
    def _has_visible_name(cls, value: str) -> str:
        validate_external_name(value)
        # Preserve the exact adapter value; normalization belongs to the
        # persistence boundary and is returned separately.
        return value


class ExerciseAliasExerciseItem(StrictExerciseAliasModel):
    id: int
    name: str
    category: str
    main_muscle_group: str
    secondary_muscle_groups: list[str]
    equipment_needed: list[str]
    source: str


class ExerciseAliasResponse(StrictExerciseAliasModel):
    source: ExerciseAliasSource
    external_name: str
    normalized_external_name: str
    exercise_id: int
    exercise: ExerciseAliasExerciseItem
