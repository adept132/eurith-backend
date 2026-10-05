"""Public metadata contracts for private body progress photos."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BodyPhotoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    taken_on: date
    angle: str
    mime_type: str
    byte_size: int
    width: int
    height: int
    created_at: datetime


class BodyPhotoPage(BaseModel):
    items: list[BodyPhotoRead]
    next_cursor: str | None = None
