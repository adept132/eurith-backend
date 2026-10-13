"""Validated, versioned rest timer formula stored in profile settings."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


BaseSeconds = Annotated[int, Field(strict=True, ge=60, le=600, multiple_of=15)]
EffortSeconds = Annotated[int, Field(strict=True, ge=-60, le=180, multiple_of=15)]


class _RestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RestBaseSeconds(_RestModel):
    tier1: BaseSeconds = 180
    tier2: BaseSeconds = 120
    tier3: BaseSeconds = 90


class RestEffortSeconds(_RestModel):
    light: EffortSeconds = -30
    medium: EffortSeconds = 0
    prefailure: EffortSeconds = 30
    failure: EffortSeconds = 60


class _PatchModel(_RestModel):
    @field_validator("*", mode="before", check_fields=False)
    @classmethod
    def explicit_null_is_invalid(cls, value):
        if value is None:
            raise ValueError("Rest timer policy fields cannot be null")
        return value


class RestBaseSecondsPatch(_PatchModel):
    tier1: BaseSeconds | None = None
    tier2: BaseSeconds | None = None
    tier3: BaseSeconds | None = None


class RestEffortSecondsPatch(_PatchModel):
    light: EffortSeconds | None = None
    medium: EffortSeconds | None = None
    prefailure: EffortSeconds | None = None
    failure: EffortSeconds | None = None


class RestTimerPolicy(_RestModel):
    version: Literal[1] = 1
    base_seconds: RestBaseSeconds = Field(default_factory=RestBaseSeconds)
    effort_seconds: RestEffortSeconds = Field(default_factory=RestEffortSeconds)

    @field_validator("version", mode="before")
    @classmethod
    def version_is_integer(cls, value):
        if type(value) is not int:
            raise ValueError("Rest timer policy version must be integer")
        return value


class RestTimerPolicyPatch(_PatchModel):
    base_seconds: RestBaseSecondsPatch | None = None
    effort_seconds: RestEffortSecondsPatch | None = None
