"""Validated request and response contracts for owner gym settings."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.services.equipment import CANONICAL


LoadMode = Literal["stack", "plate_loaded"]
WeightUnit = Literal["kg", "lb"]
WeightBasis = Literal["displayed", "plates_only", "including_start_weight"]
ExerciseSource = Literal["global", "user"]

_EQUIPMENT_CODES = CANONICAL | {"plate", "ab_roller", "box", "exercise_ball"}


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeightedCount(_Schema):
    weight: float = Field(gt=0, allow_inf_nan=False)
    count: int = Field(strict=True, ge=0)
    unit: WeightUnit


class PlateInventoryItem(_Schema):
    weight: float = Field(gt=0, allow_inf_nan=False)
    count: int = Field(strict=True, ge=0)


class GymStep(_Schema):
    category: Literal["block", "plate", "dumbbell"]
    unit: WeightUnit
    value: float = Field(gt=0, allow_inf_nan=False)


class GymProfilePayload(_Schema):
    name: str = Field(min_length=1, max_length=120)
    equipment: list[str] = Field(default_factory=list)
    bars: list[WeightedCount] = Field(default_factory=list)
    discs: list[WeightedCount] = Field(default_factory=list)
    steps: list[GymStep] = Field(default_factory=list)
    expected_revision: int = Field(ge=0)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("equipment")
    @classmethod
    def check_equipment_codes(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - _EQUIPMENT_CODES)
        if unknown:
            raise ValueError(f"unknown equipment code(s): {', '.join(unknown)}")
        return values


class GymProfileView(_Schema):
    id: UUID
    name: str
    equipment: list[str]
    bars: list[WeightedCount]
    discs: list[WeightedCount]
    steps: list[GymStep]
    revision: int = Field(ge=1)
    is_active: bool


class GymExerciseSetupPayload(_Schema):
    id: UUID
    is_available: bool
    is_preferred: bool
    step_value: float = Field(gt=0, allow_inf_nan=False)
    step_unit: WeightUnit
    loading_sides: Literal[1, 2]
    base_weight: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    weight_basis: WeightBasis
    plate_inventory: list[PlateInventoryItem] | None = None
    expected_revision: int = Field(ge=0)

    def bind_mode(self, mode: str) -> "BoundGymExerciseSetup":
        """Bind and validate the URL-owned mode before passing data to a service."""
        return BoundGymExerciseSetup.model_validate({**self.model_dump(), "mode": mode})


class BoundGymExerciseSetup(_Schema):
    """Setup request validated together with its URL-owned loading mode."""

    id: UUID
    mode: LoadMode
    is_available: bool
    is_preferred: bool
    step_value: float = Field(gt=0, allow_inf_nan=False)
    step_unit: WeightUnit
    loading_sides: Literal[1, 2]
    base_weight: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    weight_basis: WeightBasis
    plate_inventory: list[PlateInventoryItem] | None = None
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def check_mode_basis(self) -> "BoundGymExerciseSetup":
        _check_mode_basis(self.mode, self.weight_basis, self.base_weight)
        return self


class GymExerciseSetupView(_Schema):
    id: UUID
    gym_id: UUID
    exercise_source: ExerciseSource
    exercise_id: int = Field(gt=0)
    mode: LoadMode
    is_available: bool
    is_preferred: bool
    step_value: float = Field(gt=0, allow_inf_nan=False)
    step_unit: WeightUnit
    loading_sides: Literal[1, 2]
    base_weight: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    weight_basis: WeightBasis
    plate_inventory: list[PlateInventoryItem] | None = None
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def check_mode_basis(self) -> "GymExerciseSetupView":
        _check_mode_basis(self.mode, self.weight_basis, self.base_weight)
        return self


class ExerciseLoadPreferencePayload(_Schema):
    id: UUID
    enabled_modes: list[LoadMode] = Field(min_length=1)
    preferred_mode: LoadMode
    expected_revision: int = Field(ge=0)

    @field_validator("enabled_modes")
    @classmethod
    def reject_duplicate_modes(cls, modes: list[LoadMode]) -> list[LoadMode]:
        if len(set(modes)) != len(modes):
            raise ValueError("enabled_modes must not contain duplicates")
        return modes

    @model_validator(mode="after")
    def preferred_mode_must_be_enabled(self) -> "ExerciseLoadPreferencePayload":
        if self.preferred_mode not in self.enabled_modes:
            raise ValueError("preferred_mode must be present in enabled_modes")
        return self


class ExerciseLoadPreferenceView(_Schema):
    id: UUID
    exercise_source: ExerciseSource
    exercise_id: int = Field(gt=0)
    enabled_modes: list[LoadMode] = Field(min_length=1)
    preferred_mode: LoadMode
    revision: int = Field(ge=1)

    @field_validator("enabled_modes")
    @classmethod
    def reject_duplicate_modes(cls, modes: list[LoadMode]) -> list[LoadMode]:
        if len(set(modes)) != len(modes):
            raise ValueError("enabled_modes must not contain duplicates")
        return modes

    @model_validator(mode="after")
    def preferred_mode_must_be_enabled(self) -> "ExerciseLoadPreferenceView":
        if self.preferred_mode not in self.enabled_modes:
            raise ValueError("preferred_mode must be present in enabled_modes")
        return self


class ActiveGymPayload(_Schema):
    gym_profile_id: UUID | None


class RevisionedDeletePayload(_Schema):
    expected_revision: int = Field(ge=0)


def _check_mode_basis(mode: LoadMode, basis: WeightBasis, base_weight: float | None) -> None:
    if mode == "stack" and basis != "displayed":
        raise ValueError("stack setups require weight_basis='displayed'")
    if mode == "plate_loaded" and basis == "displayed":
        raise ValueError("plate_loaded setups require a plate weight basis")
    if basis == "including_start_weight" and base_weight is None:
        raise ValueError("including_start_weight requires a known base_weight")
