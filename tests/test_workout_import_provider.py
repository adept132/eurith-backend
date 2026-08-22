from __future__ import annotations

import json

import httpx
import pytest

from api.config import WorkoutImportVisionSettings
from api.schemas.workout_import import WorkoutImportMlRequest
from api.services.workout_import.openai_compatible import OpenAICompatibleWorkoutVisionProvider
from api.services.workout_import.types import (
    ProviderErrorCode,
    VisionImage,
    WorkoutVisionProviderError,
    WorkoutVisionRequest,
)


def provider_request() -> WorkoutVisionRequest:
    draft = WorkoutImportMlRequest.model_validate(
        {
            "local_ocr_text": "Bench 80 x ?",
            "local_sources": [
                {"source_id": "set-1", "source_type": "set", "raw_text": "80 x ?", "set_type": "normal"}
            ],
            "unresolved_source_ids": ["set-1"],
            "parser_warnings": ["reps unresolved"],
            "unit_preference": "kg",
        }
    )
    return WorkoutVisionRequest(
        request_id="req-safe-123",
        draft=draft,
        images=(VisionImage(content_type="image/png", body=b"not-a-real-image"),),
    )


def settings(**overrides) -> WorkoutImportVisionSettings:
    values = {"base_url": "https://vision.example.test", "api_key": "secret-key", "model": "vision-model"}
    values.update(overrides)
    return WorkoutImportVisionSettings(**values)


def success_response(result: dict | None = None) -> dict:
    result = result or {
        "suggestions": [
            {
                "source_id": "set-1",
                "field": "reps",
                "value": 8,
                "confidence": 0.9,
                "evidence_text": "80 x 8",
                "evidence": [
                    {
                        "image_index": 0,
                        "block_id": "block-1",
                        "box": {"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.1},
                    }
                ],
                "warnings": [],
            }
        ],
        "warnings": [],
    }
    return {
        "id": "resp_1",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": json.dumps(result)}],
            }
        ],
    }


@pytest.mark.asyncio
async def test_adapter_sends_current_responses_contract_and_parses_strict_output():
    seen = {}

    async def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json=success_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleWorkoutVisionProvider(settings(), client=client)
    result = await provider.extract(provider_request())
    await client.aclose()

    assert result.suggestions[0].value == 8
    assert seen["url"] == "https://vision.example.test/v1/responses"
    assert seen["authorization"] == "Bearer secret-key"
    payload = seen["payload"]
    assert payload["model"] == "vision-model"
    assert payload["store"] is False
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert "default" not in json.dumps(payload["text"]["format"]["schema"])
    content = payload["input"][0]["content"]
    assert content[0]["type"] == "input_text"
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")


def test_missing_or_partial_configuration_disables_provider(monkeypatch):
    for key in (
        "WORKOUT_IMPORT_VISION_BASE_URL",
        "WORKOUT_IMPORT_VISION_API_KEY",
        "WORKOUT_IMPORT_VISION_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    assert WorkoutImportVisionSettings.from_env() is None
    monkeypatch.setenv("WORKOUT_IMPORT_VISION_BASE_URL", "https://vision.example.test")
    assert WorkoutImportVisionSettings.from_env() is None


@pytest.mark.asyncio
async def test_invalid_configuration_is_treated_as_disabled(monkeypatch):
    from api.routers.workout_import import get_workout_vision_provider

    monkeypatch.setenv("WORKOUT_IMPORT_VISION_BASE_URL", "not-a-url")
    monkeypatch.setenv("WORKOUT_IMPORT_VISION_API_KEY", "secret")
    monkeypatch.setenv("WORKOUT_IMPORT_VISION_MODEL", "model")
    dependency = get_workout_vision_provider()
    assert await anext(dependency) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(429, ProviderErrorCode.RATE_LIMITED, True), (500, ProviderErrorCode.UNAVAILABLE, True), (400, ProviderErrorCode.UNAVAILABLE, False)],
)
async def test_http_errors_map_without_leaking_provider_body(status, code, retryable):
    async def handler(_request):
        return httpx.Response(status, text="sensitive provider body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleWorkoutVisionProvider(settings(), client=client)
        with pytest.raises(WorkoutVisionProviderError) as caught:
            await provider.extract(provider_request())
    assert caught.value.code == code
    assert caught.value.retryable is retryable
    assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_timeout_maps_to_retryable_timeout():
    async def handler(request):
        raise httpx.ReadTimeout("body must not be logged", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleWorkoutVisionProvider(settings(), client=client)
        with pytest.raises(WorkoutVisionProviderError) as caught:
            await provider.extract(provider_request())
    assert caught.value.code == ProviderErrorCode.TIMEOUT
    assert caught.value.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json", headers={"content-type": "application/json"}),
        httpx.Response(200, json={"status": "completed", "output": []}),
        httpx.Response(200, json=success_response({"suggestions": [{"source_id": "set-1", "field": "notes"}], "warnings": []})),
        httpx.Response(200, json=success_response({"suggestions": [], "warnings": [], "autosave": True})),
    ],
)
async def test_malformed_or_schema_violating_provider_output_is_rejected(response):
    async def handler(_request):
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleWorkoutVisionProvider(settings(), client=client)
        with pytest.raises(WorkoutVisionProviderError) as caught:
            await provider.extract(provider_request())
    assert caught.value.code == ProviderErrorCode.INVALID_RESPONSE
    assert caught.value.retryable is False


def test_default_client_timeout_is_exactly_thirty_seconds():
    provider = OpenAICompatibleWorkoutVisionProvider(settings())
    assert provider.timeout_seconds == 30.0


@pytest.mark.asyncio
async def test_provider_closes_only_the_client_it_owns():
    owned = OpenAICompatibleWorkoutVisionProvider(settings())
    owned_client = owned._client
    await owned.aclose()
    assert owned_client.is_closed

    external_client = httpx.AsyncClient()
    external = OpenAICompatibleWorkoutVisionProvider(settings(), client=external_client)
    await external.aclose()
    assert not external_client.is_closed
    await external_client.aclose()
