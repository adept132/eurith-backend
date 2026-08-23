from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ExerciseAliasSource = Literal["strong", "hevy", "fitbod", "table", "notes"]


class StrictExerciseAliasModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExerciseAliasUpsertRequest(StrictExerciseAliasModel):
    source: ExerciseAliasSource
    external_name: str = Field(min_length=1, max_length=200)
    exercise_id: int = Field(gt=0)

    @field_validator("external_name")
    @classmethod
    def _has_visible_name(cls, value: str) -> str:
        if not " ".join(value.split()):
            raise ValueError("external_name must contain non-whitespace text")
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
