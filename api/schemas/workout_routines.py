from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from api.schemas.workouts import ExerciseShortResponse


RoutineSetKind = Literal["normal", "amrap", "backoff"]


def _trimmed(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


class WorkoutRoutineExerciseWrite(BaseModel):
    exercise_id: int = Field(gt=0)
    order_index: int = Field(ge=0)
    superset_group: str | None = Field(default=None, max_length=64)
    target_sets: int = Field(gt=0, le=100)
    set_kinds: list[RoutineSetKind] = Field(min_length=1, max_length=100)
    rep_min: int | None = Field(default=None, gt=0, le=1000)
    rep_max: int | None = Field(default=None, gt=0, le=1000)
    target_rir: int | None = Field(default=None, ge=0, le=10)
    rest_seconds: int | None = Field(default=None, ge=0, le=86400)
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("superset_group", "notes")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return _trimmed(value)

    @model_validator(mode="after")
    def validate_ranges_and_kinds(self) -> "WorkoutRoutineExerciseWrite":
        if self.rep_min is not None and self.rep_max is not None and self.rep_max < self.rep_min:
            raise ValueError("rep_max must be greater than or equal to rep_min")
        if len(self.set_kinds) != self.target_sets:
            raise ValueError("set_kinds length must match target_sets")
        return self


def _validate_contiguous(exercises: list[WorkoutRoutineExerciseWrite]) -> list[WorkoutRoutineExerciseWrite]:
    order = sorted(item.order_index for item in exercises)
    if order != list(range(len(exercises))):
        raise ValueError("exercise order_index values must be contiguous from zero")
    return exercises


class WorkoutRoutineCreate(BaseModel):
    client_uuid: str | None = Field(default=None, min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=4000)
    sort_order: int = Field(default=0, ge=0)
    exercises: list[WorkoutRoutineExerciseWrite] = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = _trimmed(value)
        if normalized is None:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str | None) -> str | None:
        return _trimmed(value)

    @field_validator("exercises")
    @classmethod
    def validate_order(cls, value: list[WorkoutRoutineExerciseWrite]):
        return _validate_contiguous(value)


class WorkoutRoutineUpdate(BaseModel):
    base_revision: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=4000)
    sort_order: int | None = Field(default=None, ge=0)
    exercises: list[WorkoutRoutineExerciseWrite] | None = Field(
        default=None, min_length=1, max_length=100
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _trimmed(value)
        if normalized is None:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str | None) -> str | None:
        return _trimmed(value)

    @field_validator("exercises")
    @classmethod
    def validate_order(cls, value: list[WorkoutRoutineExerciseWrite] | None):
        return _validate_contiguous(value) if value is not None else None


class SaveWorkoutAsRoutineRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    client_uuid: str | None = Field(default=None, min_length=1, max_length=64)
    sort_order: int = Field(default=0, ge=0)
    persistent_note_ids: list[str] = Field(default_factory=list, max_length=101)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = _trimmed(value)
        if normalized is None:
            raise ValueError("name must not be blank")
        return normalized


class WorkoutRoutineExerciseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    exercise_id: int
    order_index: int
    superset_group: str | None
    target_sets: int
    set_kinds: list[RoutineSetKind]
    rep_min: int | None
    rep_max: int | None
    target_rir: int | None
    rest_seconds: int | None
    notes: str | None
    exercise: ExerciseShortResponse


class WorkoutRoutineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_uuid: str
    name: str
    notes: str | None
    sort_order: int
    revision: int
    created_at: datetime
    updated_at: datetime
    exercises: list[WorkoutRoutineExerciseRead]
