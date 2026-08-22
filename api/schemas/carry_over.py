from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


CarryOverStatus = Literal["draft", "applied", "dismissed", "expired"]
CarryOverMatchKind = Literal["exact", "compatible"]
CarryOverDiscardReason = Literal[
    "session_cap",
    "mrv",
    "deload",
    "readiness",
    "pain",
    "equipment",
    "microcycle_end",
    "no_compatible_day",
]


class CarryOverPlacement(BaseModel):
    calendar_day_id: int = Field(gt=0)
    target_date: date
    exercise_id: int = Field(gt=0)
    delta_sets: int = Field(gt=0)
    match_kind: CarryOverMatchKind


class CarryOverDiscard(BaseModel):
    sets: int = Field(gt=0)
    reason: CarryOverDiscardReason


class CarryOverPreviewItem(BaseModel):
    carry_over_id: int = Field(gt=0)
    source_exercise_id: int = Field(gt=0)
    requested_sets: int = Field(ge=0)
    placed_sets: int = Field(ge=0)
    discarded_sets: int = Field(ge=0)
    placements: list[CarryOverPlacement] = Field(default_factory=list)
    discarded: list[CarryOverDiscard] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> "CarryOverPreviewItem":
        if self.requested_sets != self.placed_sets + self.discarded_sets:
            raise ValueError("requested_sets must equal placed_sets + discarded_sets")
        if sum(item.delta_sets for item in self.placements) != self.placed_sets:
            raise ValueError("placements must sum to placed_sets")
        if sum(item.sets for item in self.discarded) != self.discarded_sets:
            raise ValueError("discard reasons must sum to discarded_sets")
        return self


class UnfinishedExerciseSets(BaseModel):
    source_exercise_id: int = Field(gt=0)
    requested_sets: int = Field(gt=0)


class CarryOverPreviewRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64)
    unfinished_sets: list[UnfinishedExerciseSets] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_source_exercises(self) -> "CarryOverPreviewRequest":
        ids = [item.source_exercise_id for item in self.unfinished_sets]
        if len(ids) != len(set(ids)):
            raise ValueError("source_exercise_id values must be unique")
        return self


class CarryOverDecisionRequest(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)


class CarryOverResponse(BaseModel):
    status: CarryOverStatus
    source_session_id: int = Field(gt=0)
    microcycle_end: date
    preview_token: str = Field(min_length=64, max_length=64)
    items: list[CarryOverPreviewItem]
