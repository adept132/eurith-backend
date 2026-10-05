from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from api.schemas.workout_import import WorkoutImportMlRequest


@dataclass(frozen=True, slots=True)
class VisionImage:
    content_type: str
    body: bytes


@dataclass(frozen=True, slots=True)
class WorkoutVisionRequest:
    request_id: str
    draft: WorkoutImportMlRequest
    images: tuple[VisionImage, ...]


class ProviderErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"


class WorkoutVisionProviderError(Exception):
    def __init__(self, code: ProviderErrorCode, *, retryable: bool):
        super().__init__(code.value)
        self.code = code
        self.retryable = retryable
