from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


PlanReplacementSourceKind = Literal["workout", "routine"]


class ComparedExercise(BaseModel):
    exercise_id: int
    exercise_name: str
    old_order_index: int | None = None
    new_order_index: int | None = None
    old_target_sets: int | None = None
    new_target_sets: int | None = None
    old_rep_min: int | None = None
    new_rep_min: int | None = None
    old_rep_max: int | None = None
    new_rep_max: int | None = None
    old_target_rir: int | None = None
    new_target_rir: int | None = None
    changed_fields: list[str] = Field(default_factory=list)


class PlanReplacementPreview(BaseModel):
    target_plan_id: int
    source_kind: PlanReplacementSourceKind
    source_id: int
    effective_from: date
    affected_dates: list[date]
    old_total_sets: int
    new_total_sets: int
    added: list[ComparedExercise]
    removed: list[ComparedExercise]
    modified: list[ComparedExercise]
    preview_token: str


class PlanReplacementPreviewRequest(BaseModel):
    target_plan_id: int = Field(gt=0)


class PlanReplacementApplyRequest(BaseModel):
    target_plan_id: int = Field(gt=0)
    preview_token: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=64)


class PlanReplacementResult(BaseModel):
    status: Literal["applied"] = "applied"
    new_plan_id: int
    affected_dates: list[date]
