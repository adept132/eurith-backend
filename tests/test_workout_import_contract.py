from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.schemas.workout_import import (
    MlWorkoutImportResult,
    WorkoutImportMlRequest,
)


def source(source_id: str = "set-1", **overrides):
    value = {
        "source_id": source_id,
        "source_type": "set",
        "raw_text": "80 x ?",
        "set_type": "normal",
    }
    value.update(overrides)
    return value


def request(**overrides):
    value = {
        "local_ocr_text": "Bench press\n80 x ?",
        "local_sources": [source()],
        "unresolved_source_ids": ["set-1"],
        "parser_warnings": ["reps unresolved"],
        "unit_preference": "kg",
        "ocr_blocks": [
            {
                "image_index": 0,
                "block_id": "image-0-block-2",
                "text": "80 x ?",
                "box": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
            }
        ],
    }
    value.update(overrides)
    return value


def evidence():
    return {
        "image_index": 0,
        "block_id": "image-0-block-2",
        "box": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.1},
    }


def suggestion(field="reps", value=8, **overrides):
    item = {
        "source_id": "set-1",
        "field": field,
        "value": value,
        "confidence": 0.91,
        "evidence_text": "80 x 8",
        "evidence": [evidence()],
        "warnings": [],
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    "payload",
    [
        request(unknown=True),
        request(local_sources=[source(), source()]),
        request(local_sources=[source(set_type="cluster")]),
        request(unresolved_source_ids=["missing"]),
    ],
)
def test_request_rejects_unknown_duplicate_or_unsupported_input(payload):
    with pytest.raises(ValidationError):
        WorkoutImportMlRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("weight", -1),
        ("weight", float("nan")),
        ("reps", 0),
        ("reps", 2.5),
        ("unit", "stone"),
        ("exercise_name", ""),
        ("bodyweight", "yes"),
        ("assisted", 1),
        ("notes", "provider must not add this"),
    ],
)
def test_result_rejects_invalid_or_unsupported_patch_values(field, value):
    with pytest.raises(ValidationError):
        MlWorkoutImportResult.model_validate({"suggestions": [suggestion(field, value)], "warnings": []})


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan")])
def test_result_rejects_confidence_outside_closed_unit_interval(confidence):
    with pytest.raises(ValidationError):
        MlWorkoutImportResult.model_validate(
            {"suggestions": [suggestion(confidence=confidence)], "warnings": []}
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"evidence": []},
        {"evidence_text": ""},
        {"evidence": [{**evidence(), "image_index": 3}]},
        {"unknown": "payload"},
    ],
)
def test_result_requires_bounded_source_evidence_and_forbids_extra_fields(patch):
    with pytest.raises(ValidationError):
        MlWorkoutImportResult.model_validate(
            {"suggestions": [suggestion(**patch)], "warnings": []}
        )


def test_empty_and_partial_results_are_valid():
    assert MlWorkoutImportResult(suggestions=[], warnings=[]).suggestions == []
    parsed = MlWorkoutImportResult.model_validate(
        {"suggestions": [suggestion("exercise_name", "Bench press")], "warnings": ["review"]}
    )
    assert parsed.suggestions[0].source_id == "set-1"


def test_request_accepts_partial_local_result():
    parsed = WorkoutImportMlRequest.model_validate(
        request(local_ocr_text="", parser_warnings=[], local_sources=[source(raw_text="?")])
    )
    assert parsed.unresolved_source_ids == ["set-1"]


def test_request_rejects_duplicate_or_out_of_bounds_ocr_blocks():
    duplicate = request(ocr_blocks=[request()["ocr_blocks"][0], request()["ocr_blocks"][0]])
    invalid = request(ocr_blocks=[{**request()["ocr_blocks"][0], "image_index": 3}])
    with pytest.raises(ValidationError):
        WorkoutImportMlRequest.model_validate(duplicate)
    with pytest.raises(ValidationError):
        WorkoutImportMlRequest.model_validate(invalid)
