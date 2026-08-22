from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
SetType = Literal["normal", "warmup", "drop"]
PatchField = Literal["exercise_name", "weight", "reps", "unit", "bodyweight", "assisted"]


class StrictWorkoutImportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OcrBox(StrictWorkoutImportModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside_image(self) -> "OcrBox":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("OCR box must be contained in the image")
        return self


class OcrEvidence(StrictWorkoutImportModel):
    image_index: int = Field(ge=0, lt=3)
    block_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    box: OcrBox


class OcrInputBlock(StrictWorkoutImportModel):
    image_index: int = Field(ge=0, lt=3)
    block_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    text: str = Field(max_length=2000)
    box: OcrBox


class LocalWorkoutSource(StrictWorkoutImportModel):
    source_id: SourceId
    source_type: Literal["exercise", "set"]
    raw_text: str = Field(max_length=4000)
    set_type: SetType | None = None

    @model_validator(mode="after")
    def _set_type_matches_source(self) -> "LocalWorkoutSource":
        if self.source_type == "set" and self.set_type is None:
            raise ValueError("set sources require set_type")
        if self.source_type == "exercise" and self.set_type is not None:
            raise ValueError("exercise sources cannot carry set_type")
        return self


class WorkoutImportMlRequest(StrictWorkoutImportModel):
    local_ocr_text: str = Field(max_length=50_000)
    local_sources: list[LocalWorkoutSource] = Field(max_length=500)
    unresolved_source_ids: list[SourceId] = Field(min_length=1, max_length=500)
    parser_warnings: list[Annotated[str, StringConstraints(max_length=500)]] = Field(max_length=100)
    unit_preference: Literal["kg", "lb"]
    ocr_blocks: list[OcrInputBlock] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def _validate_source_ids(self) -> "WorkoutImportMlRequest":
        source_ids = [item.source_id for item in self.local_sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("local source ids must be unique")
        if len(self.unresolved_source_ids) != len(set(self.unresolved_source_ids)):
            raise ValueError("unresolved source ids must be unique")
        if not set(self.unresolved_source_ids).issubset(source_ids):
            raise ValueError("unresolved source ids must reference local sources")
        block_keys = [(item.image_index, item.block_id) for item in self.ocr_blocks]
        if len(block_keys) != len(set(block_keys)):
            raise ValueError("OCR block ids must be unique within an image")
        return self


PatchValue = str | int | float | bool


class MlWorkoutPatch(StrictWorkoutImportModel):
    source_id: SourceId
    field: PatchField
    value: PatchValue
    confidence: float = Field(ge=0, le=1)
    evidence_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    evidence: list[OcrEvidence] = Field(min_length=1, max_length=12)
    warnings: list[Annotated[str, StringConstraints(max_length=500)]] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _validate_field_value(self) -> "MlWorkoutPatch":
        value = self.value
        if not math.isfinite(self.confidence):
            raise ValueError("confidence must be finite")
        if self.field == "exercise_name":
            if type(value) is not str or not value.strip() or len(value.strip()) > 160:
                raise ValueError("exercise_name must be non-empty text")
        elif self.field == "weight":
            if type(value) not in (int, float) or not math.isfinite(float(value)) or not 0 <= float(value) <= 2000:
                raise ValueError("weight must be finite and between 0 and 2000")
        elif self.field == "reps":
            if type(value) is not int or not 1 <= value <= 1000:
                raise ValueError("reps must be an integer between 1 and 1000")
        elif self.field == "unit":
            if type(value) is not str or value not in ("kg", "lb"):
                raise ValueError("unit must be kg or lb")
        elif self.field in ("bodyweight", "assisted"):
            if type(value) is not bool:
                raise ValueError(f"{self.field} must be boolean")
        return self


class MlWorkoutImportResult(StrictWorkoutImportModel):
    suggestions: list[MlWorkoutPatch] = Field(default_factory=list, max_length=1000)
    warnings: list[Annotated[str, StringConstraints(max_length=500)]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _unique_patches(self) -> "MlWorkoutImportResult":
        keys = [(item.source_id, item.field) for item in self.suggestions]
        if len(keys) != len(set(keys)):
            raise ValueError("only one suggestion per source field is allowed")
        return self


class WorkoutImportMlFallbackResponse(StrictWorkoutImportModel):
    request_id: str
    draft_id: str
    status: Literal["review_required"] = "review_required"
    provenance: Literal["ml_fallback"] = "ml_fallback"
    suggestions: list[MlWorkoutPatch]
    warnings: list[str]
