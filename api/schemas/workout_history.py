from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from api.schemas.workouts import (
    WorkoutEffortLevel,
    WorkoutSessionDetailResponse,
    WorkoutSetType,
    WorkoutSource,
)


WorkoutEntryMode = Literal["live", "manual", "screenshot_import"]
RecalculationStatus = Literal["pending", "processing", "completed", "failed"]
ClientUuid = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class StrictHistoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkoutHistorySetDraft(StrictHistoryModel):
    client_uuid: ClientUuid
    parent_client_uuid: ClientUuid | None = None
    set_number: int = Field(ge=1)
    set_type: WorkoutSetType = "normal"
    weight: Decimal | None = Field(default=None, ge=0, le=2000)
    reps: int | None = Field(default=None, ge=0, le=1000)
    effort_level: WorkoutEffortLevel | None = None
    notes: str | None = None
    is_completed: bool = True

    _normalize_notes = field_validator("notes", mode="before")(_blank_to_none)


class WorkoutHistoryExerciseDraft(StrictHistoryModel):
    client_uuid: ClientUuid
    exercise_id: int = Field(gt=0)
    order_index: int = Field(ge=0)
    superset_group: str | None = Field(default=None, max_length=64)
    notes: str | None = None
    sets: list[WorkoutHistorySetDraft] = Field(default_factory=list)

    _normalize_notes = field_validator("notes", mode="before")(_blank_to_none)
    _normalize_superset = field_validator("superset_group", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def _unique_sets(self) -> "WorkoutHistoryExerciseDraft":
        numbers = [item.set_number for item in self.sets]
        uuids = [item.client_uuid for item in self.sets]
        if len(numbers) != len(set(numbers)):
            raise ValueError("set_number must be unique within an exercise")
        if len(uuids) != len(set(uuids)):
            raise ValueError("set client_uuid must be unique within an exercise")
        uuid_set = set(uuids)
        for item in self.sets:
            parent_uuid = item.parent_client_uuid
            if parent_uuid is None:
                continue
            if parent_uuid == item.client_uuid:
                raise ValueError(
                    f"parent_client_uuid '{parent_uuid}' cannot reference the same set"
                )
            if parent_uuid not in uuid_set:
                raise ValueError(
                    f"parent_client_uuid '{parent_uuid}' must reference a set in the same exercise"
                )
        return self


class WorkoutHistoryDraft(StrictHistoryModel):
    client_uuid: ClientUuid
    base_revision: int = Field(ge=0)
    entry_mode: WorkoutEntryMode
    started_at: datetime
    finished_at: datetime
    notes: str | None = None
    session_rpe: float | None = Field(default=None, ge=0, le=10)
    exercises: list[WorkoutHistoryExerciseDraft] = Field(default_factory=list)

    _normalize_notes = field_validator("notes", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "WorkoutHistoryDraft":
        if self.finished_at <= self.started_at:
            raise ValueError("finished_at must be after started_at")
        uuids = [item.client_uuid for item in self.exercises]
        order_indexes = [item.order_index for item in self.exercises]
        if len(uuids) != len(set(uuids)):
            raise ValueError("exercise client_uuid must be unique")
        if len(order_indexes) != len(set(order_indexes)):
            raise ValueError("exercise order_index must be unique")
        return self


class WorkoutHistoryServerSetSnapshot(WorkoutHistorySetDraft):
    # Older completed sessions may predate strict history-entry validation.
    # Keep those snapshots readable while new writes require 1..1000 reps.
    reps: int | None = Field(default=None, ge=0, le=1000)


class WorkoutHistoryServerExerciseSnapshot(WorkoutHistoryExerciseDraft):
    exercise_name: str | None = Field(default=None, min_length=1, max_length=255)
    sets: list[WorkoutHistoryServerSetSnapshot] = Field(default_factory=list)


class WorkoutHistoryServerSnapshot(WorkoutHistoryDraft):
    exercises: list[WorkoutHistoryServerExerciseSnapshot] = Field(default_factory=list)


class WorkoutHistoryListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_uuid: str | None = None
    source: WorkoutSource
    entry_mode: WorkoutEntryMode
    revision: int
    started_at: datetime
    finished_at: datetime
    edited_at: datetime | None = None
    notes: str | None = None
    session_rpe: float | None = None
    exercises_count: int = 0
    sets_count: int = 0


class WorkoutHistorySaveResponse(BaseModel):
    workout: WorkoutSessionDetailResponse
    recalculation_status: RecalculationStatus


class WorkoutHistoryListResponse(BaseModel):
    workouts: list[WorkoutHistoryListItem]
