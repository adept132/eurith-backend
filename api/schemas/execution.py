from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class ExecutionModeState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1]
    enabled: bool
    preference_epoch: str = Field(min_length=1, max_length=64)
    initialized_at: datetime
    changed_at: datetime
    banner_dismissed_at: datetime | None = None

    @field_validator('initialized_at', 'changed_at', 'banner_dismissed_at')
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('Execution timestamps require timezone')
        return value


class ExecutionSlot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    state: Literal['forecast', 'draft']
    set_client_uuid: UUID | None = None
    updated_at: datetime

    @model_validator(mode='after')
    def valid_binding(self):
        if self.updated_at.tzinfo is None:
            raise ValueError('Execution timestamps require timezone')
        if (self.state == 'draft') != (self.set_client_uuid is not None):
            raise ValueError('Only a draft binds a real set UUID')
        return self


class ExecutionExerciseState(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: Literal[1]
    exercise_id: int = Field(gt=0)
    slots: dict[str, ExecutionSlot] = Field(default_factory=dict, max_length=1000)

    @model_validator(mode='after')
    def valid_slots(self):
        identities = set()
        for key, slot in self.slots.items():
            if not key.isascii() or not key.isdigit() or str(int(key)) != key or not 1 <= int(key) <= 1000:
                raise ValueError('Invalid planned set number')
            if slot.set_client_uuid is not None:
                if slot.set_client_uuid in identities:
                    raise ValueError('One set cannot occupy multiple planned slots')
                identities.add(slot.set_client_uuid)
        return self
