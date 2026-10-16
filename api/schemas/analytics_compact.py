"""Opt-in lossless transport v1. Tuple positions are a versioned wire contract."""
from datetime import date, datetime
from typing import Any, Literal
from .analytics import (DTO, PointDetails, ExercisePoint, ExerciseAnalytics,
    MuscleContribution, MuscleProgress, MuscleAnalytics, BlockAnalytics, BlockComparison)

# SetObservation field order, with intensity/context values replaced by local refs.
ObservationRow = tuple[int, int, int, str | None, str | None, str | None,
    datetime, date, str, int | None, int | None, float | None, float | None,
    float | None, int, int | None, int | None, tuple[str, ...]]
MetricRow = tuple[float | None, str | None, tuple[int, int] | None]


class CompactPointDetails(PointDetails):
    observations: list[ObservationRow]


class CompactPoint(ExercisePoint):
    details: CompactPointDetails


class CompactExercise(ExerciseAnalytics):
    points: list[int]


class CompactContribution(MuscleContribution):
    observations: tuple[int, ...]


class CompactMuscle(MuscleProgress):
    contributions: tuple[CompactContribution, ...]


class CompactMuscles(MuscleAnalytics):
    muscles: list[CompactMuscle]


class CompactBlock(BlockAnalytics):
    exercises: dict[int, CompactExercise]
    muscles: list[CompactMuscle]


class CompactComparison(BlockComparison):
    a: CompactBlock
    b: CompactBlock


class AnalyticsCompactV1(DTO):
    encoding: Literal['compact-v1'] = 'compact-v1'
    schema_version: Literal[1] = 1
    resource: Literal['exercise', 'muscles', 'block', 'comparison']
    owner_uid: str
    rules_version: Literal['p1-08-v1'] = 'p1-08-v1'
    data_revision: int
    payload: CompactExercise | CompactMuscles | CompactBlock | CompactComparison
    contexts: list[dict[str, Any]]
    metrics: list[MetricRow]
    points: list[CompactPoint]
