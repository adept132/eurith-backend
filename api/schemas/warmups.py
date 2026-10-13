"""Warmup settings DTOs; null overrides inherit and omitted fields preserve."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from api.schemas.gym_profiles import (
    Collar, ExerciseSource, LoadMode, SelectedBar, WeightedCount, WeightBasis, WeightUnit,
)

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


# Snapshot facts are independent of live workout prescriptions and gym settings.
NonnegativeWeight = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class _SnapshotModel(_WarmupModel):
    @model_serializer(mode="wrap")
    def omit_absent_metadata(self, handler):
        data = handler(self)
        for name, field in type(self).model_fields.items():
            if not field.is_required() and name not in self.model_fields_set:
                data.pop(name, None)
        return data


class ResolvedWarmupSettings(_SnapshotModel):
    count: PositiveCount
    between_seconds: RestSeconds
    before_work: WarmupBeforeWorkRest
    category: Literal["compound", "isolation", "unknown"]
    count_origin: Literal["global", "exercise", "preview"]
    warnings: list[str] = Field(default_factory=list)


class WarmupStep(_SnapshotModel):
    value: float = Field(gt=0, allow_inf_nan=False)
    unit: WeightUnit


class WarmupInventory(_SnapshotModel):
    mode: Literal["limited", "unlimited"]
    items: list[WeightedCount]


class WarmupLoadContext(_SnapshotModel):
    kind: Literal["barbell", "plate_loaded", "step", "manual"]
    gym_profile_id: UUID | None
    setup_id: UUID | None
    load_mode: LoadMode | None
    loading_sides: Literal[1, 2]
    weight_basis: WeightBasis
    base_weight_kg: NonnegativeWeight | None
    bar: SelectedBar | None
    collar: Collar | None
    step: WarmupStep | None = None
    inventory: WarmupInventory


class WorkingGoal(_SnapshotModel):
    # A reserved source UUID need not have a persisted working row yet.
    set_client_uuid: UUID
    weight_kg: float = Field(gt=0, le=2000, allow_inf_nan=False)
    reps: int = Field(strict=True, gt=0, le=1000)
    rir: float = Field(ge=0, allow_inf_nan=False)
    load_context: WarmupLoadContext


class WarmupCalculationSnapshot(_SnapshotModel):
    version: UUID
    calculated_at: datetime
    algorithm_version: Literal[1, 2]
    source: WorkingGoal
    source_e1rm_kg: float = Field(gt=0, allow_inf_nan=False)
    settings: ResolvedWarmupSettings
    equipment: WarmupLoadContext


class WarmupGroupSnapshot(_SnapshotModel):
    id: UUID
    active_version: UUID | None
    versions: list[WarmupCalculationSnapshot]
    updated_at: datetime
    settings: ResolvedWarmupSettings | None = None
    equipment: WarmupLoadContext | None = None

    @model_validator(mode="after")
    def valid_versions(self):
        versions = [item.version for item in self.versions]
        if len(versions) != len(set(versions)):
            raise ValueError("calculation versions must be unique")
        if self.active_version is not None and self.active_version not in versions:
            raise ValueError("active_version must reference a calculation version")
        return self


class WarmupRowSnapshot(_SnapshotModel):
    group_id: UUID
    calculation_version: UUID | None
    order: PositiveCount
    fraction: Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)] | None
    raw_weight_kg: NonnegativeWeight | None
    manually_edited: bool = Field(strict=True)

    @model_validator(mode="after")
    def manual_has_no_calculation(self):
        if self.calculation_version is None and (self.fraction is not None or self.raw_weight_kg is not None):
            raise ValueError("manual rows cannot contain calculated provenance")
        return self


def snapshot_json(value):
    return value.model_dump(mode="json", exclude_unset=True) if value is not None else None


def validate_warmup_rows(group, rows):
    """Validate supplied non-null rows against the effective stored/sent group."""
    group = WarmupGroupSnapshot.model_validate(group) if group is not None else None
    versions = {item.version for item in group.versions} if group else set()
    seen = set()
    for row in rows:
        if row is None:
            continue
        row = WarmupRowSnapshot.model_validate(row)
        if group is None or row.group_id != group.id:
            raise ValueError("warmup row group_id must reference the exercise group")
        if row.calculation_version is not None and row.calculation_version not in versions:
            raise ValueError("unknown warmup calculation_version")
        key = (row.calculation_version, row.order)
        if key in seen:
            raise ValueError("warmup order must be unique within a calculation version")
        seen.add(key)
