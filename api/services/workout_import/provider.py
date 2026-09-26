from __future__ import annotations

from typing import Protocol

from api.schemas.workout_import import MlWorkoutImportResult
from api.services.workout_import.types import WorkoutVisionRequest


class WorkoutVisionProvider(Protocol):
    async def extract(self, request: WorkoutVisionRequest) -> MlWorkoutImportResult:
        """Return untrusted suggestions; callers must apply domain guards."""
