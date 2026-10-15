"""Versioned historical facts. None denotes unavailable context, never inferred zero."""
from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Snapshot(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    schema_version: Literal[1] = 1


class ImportProvenance(BaseModel):
    """Descriptive origin only; these identifiers never authorize local lookup."""
    model_config = ConfigDict(extra='forbid')
    original_source: Literal['journal', 'manual', 'reconstructed', 'imported'] | None = None
    original_measurement_id: Annotated[str, Field(max_length=64)] | None = None
    original_measurement_client_uuid: Annotated[str, Field(max_length=64)] | None = None
    original_captured_at: AwareDatetime | None = None
    import_format: Annotated[str, Field(max_length=64)] | None = None


class BodyMassSnapshot(Snapshot):
    weight_kg: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    measured_on: date
    measurement_id: int | None = None
    measurement_client_uuid: str | None = None
    source: Literal['journal', 'manual', 'reconstructed', 'imported']
    captured_at: AwareDatetime
    import_provenance: ImportProvenance | None = None


class WorkoutAnalyticsSnapshot(Snapshot):
    body_mass: BodyMassSnapshot | None = None
    is_deload: bool | None = None
    phase_source: str | None = None


class SetLoadSnapshot(Snapshot):
    model: Literal['barbell', 'dumbbell', 'stack', 'plate_loaded', 'bodyweight', 'partial_bodyweight']
    input_basis: Literal['full', 'plates_only', 'per_implement', 'displayed', 'body_components']
    base_weight_kg: Nonnegative | None = None
    implement_count: Annotated[int, Field(gt=0)] | None = None
    rep_basis: Literal['simultaneous', 'per_side', 'total'] | None = None
    left_reps: Annotated[int, Field(ge=0)] | None = None
    right_reps: Annotated[int, Field(ge=0)] | None = None
    added_load_kg: Nonnegative | None = None
    assistance_kg: Nonnegative | None = None
    model_id: str | None = None
    source: str | None = None


class AnomalyReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rules_version: str
    reason_codes: list[str]
    confirmed: bool
    confirmed_at: AwareDatetime | None = None
