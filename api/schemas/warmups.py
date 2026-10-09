"""Warmup settings DTOs; null overrides inherit and omitted fields preserve."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.schemas.gym_profiles import ExerciseSource

PositiveCount = Annotated[int, Field(strict=True, gt=0)]
RestSeconds = Annotated[int, Field(strict=True, ge=0)]


class _WarmupModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WarmupWorkingRest(_WarmupModel):
    mode: Literal["working"] = "working"


class WarmupSecondsRest(_WarmupModel):
    mode: Literal["seconds"]
    seconds: RestSeconds


WarmupBeforeWorkRest = Annotated[WarmupWorkingRest | WarmupSecondsRest, Field(discriminator="mode")]


class WarmupDefaults(_WarmupModel):
    compound_count: PositiveCount = 3
    isolation_count: PositiveCount = 2
    between_seconds: RestSeconds = 30
    before_work: WarmupBeforeWorkRest = Field(default_factory=WarmupWorkingRest)


class WarmupDefaultsPatch(_WarmupModel):
    compound_count: PositiveCount | None = None
    isolation_count: PositiveCount | None = None
    between_seconds: RestSeconds | None = None
    before_work: WarmupBeforeWorkRest | None = None

    @field_validator("compound_count", "isolation_count", "between_seconds", "before_work")
    @classmethod
    def explicit_null_is_invalid(cls, value):
        if value is None:
            raise ValueError("Global warmup settings cannot be null")
        return value


class WarmupOverride(_WarmupModel):
    count: PositiveCount | None = None
    between_seconds: RestSeconds | None = None
    before_work: WarmupBeforeWorkRest | None = None


class WarmupPreferencePayload(_WarmupModel):
    id: UUID
    override: WarmupOverride
    expected_revision: Annotated[int, Field(strict=True, ge=0)]


class WarmupPreferenceView(_WarmupModel):
    id: UUID
    exercise_source: ExerciseSource
    exercise_id: int
    override: WarmupOverride
    revision: int
