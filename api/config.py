from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class WorkoutImportVisionSettings:
    base_url: str
    api_key: str
    model: str

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("WORKOUT_IMPORT_VISION_BASE_URL must be an absolute HTTP(S) URL")
        if not self.api_key.strip() or not self.model.strip():
            raise ValueError("vision API key and model must be non-empty")

    @classmethod
    def from_env(cls) -> "WorkoutImportVisionSettings | None":
        values = {
            "base_url": os.getenv("WORKOUT_IMPORT_VISION_BASE_URL", "").strip(),
            "api_key": os.getenv("WORKOUT_IMPORT_VISION_API_KEY", "").strip(),
            "model": os.getenv("WORKOUT_IMPORT_VISION_MODEL", "").strip(),
        }
        if not all(values.values()):
            return None
        return cls(**values)
