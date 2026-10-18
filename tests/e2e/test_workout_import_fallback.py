from __future__ import annotations

import json
from io import BytesIO

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from PIL import Image

from api.main import app
from api.routers.workout_import import get_workout_vision_provider
from api.schemas.workout_import import MlWorkoutImportResult
from api.services.models import WorkoutSession
from api.services.workout_import.types import ProviderErrorCode, WorkoutVisionProviderError


def png(color):
    output = BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


FIXTURES = {
    "strong": png((255, 0, 0)),
    "notes": png((0, 255, 0)),
    "mixed": png((0, 0, 255)),
}


def draft(source_id="set-1"):
    return {
        "local_ocr_text": "Bench 80 x ?",
        "local_sources": [
            {"source_id": source_id, "source_type": "set", "raw_text": "80 x ?", "set_type": "normal"}
        ],
        "unresolved_source_ids": [source_id],
        "parser_warnings": ["local parser unresolved"],
        "unit_preference": "kg",
        "ocr_blocks": [
            {
                "image_index": 0,
                "block_id": "b1",
                "text": "80 x ?",
                "box": {"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.1},
            }
        ],
    }


def result(field="reps", value=8, confidence=0.93):
    return MlWorkoutImportResult.model_validate({
        "suggestions": [{
            "source_id": "set-1",
            "field": field,
            "value": value,
            "confidence": confidence,
            "evidence_text": "80 x 8",
            "evidence": [{
                "image_index": 0,
                "block_id": "b1",
                "box": {"x": 0.1, "y": 0.1, "width": 0.3, "height": 0.1},
            }],
            "warnings": [],
        }],
        "warnings": [],
    })


class FixtureProvider:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def extract(self, request):
        self.calls.append(request)
        if self.failure:
            raise self.failure
        marker = Image.open(BytesIO(request.images[0].body)).getpixel((0, 0))
        if marker == (255, 0, 0):
            return result("reps", 8)
        if marker == (0, 255, 0):
            return result("weight", 80.0, 0.88)
        return MlWorkoutImportResult(suggestions=[], warnings=["mixed_language_low_confidence"])


@pytest_asyncio.fixture
async def fixture_provider():
    provider = FixtureProvider()
    app.dependency_overrides[get_workout_vision_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_workout_vision_provider, None)


async def send(client, name, key):
    return await client.post(
        "/workout-import/ml-fallback",
        headers={"Idempotency-Key": key},
        data={"consent": "one_time", "draft": json.dumps(draft())},
        files=[("images", (f"{name}.png", FIXTURES[name], "image/png"))],
    )


@pytest.mark.asyncio
async def test_three_acceptance_fixtures_return_review_drafts_without_workout_mutation(
    client, db, fixture_provider
):
    before = await db.scalar(select(func.count()).select_from(WorkoutSession))
    responses = [await send(client, name, f"e2e-fixture-{index}") for index, name in enumerate(FIXTURES)]
    await db.rollback()
    after = await db.scalar(select(func.count()).select_from(WorkoutSession))

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert all(response.json()["status"] == "review_required" for response in responses)
    assert responses[2].json()["suggestions"] == []
    assert "mixed_language_low_confidence" in responses[2].json()["warnings"]
    assert before == after
    assert len(fixture_provider.calls) == 3


@pytest.mark.asyncio
async def test_timeout_keeps_database_free_of_workouts_and_returns_retryable_error(
    client, db, fixture_provider
):
    fixture_provider.failure = WorkoutVisionProviderError(ProviderErrorCode.TIMEOUT, retryable=True)
    response = await send(client, "strong", "e2e-timeout-1")
    assert response.status_code == 504
    assert response.json()["detail"]["retryable"] is True
    assert await db.scalar(select(func.count()).select_from(WorkoutSession)) == 0


@pytest.mark.asyncio
async def test_invalid_provider_output_never_reaches_review_or_persistence(client, fixture_provider):
    fixture_provider.extract = lambda _request: None  # type: ignore[method-assign]
    response = await send(client, "strong", "e2e-invalid-1")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_response"
