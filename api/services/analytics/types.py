"""Shared inputs and unrounded results for the analytics pipeline."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TypedDict

@dataclass(frozen=True)
class RawSetFact:
    workout_id: int
    exercise_id: int
    exercise_instance_id: int
    set_id: int
    set_number: int
    exercise_order: int
    started_at: datetime
    finished_at: datetime | None
    gym_id: str | None
    gym_label: str | None
    setup_id: str | None
    set_type: str
    is_completed: bool
    is_anomalous: bool
    weight_kg: Decimal | None
    reps: int | None
    effort_level: str | None
    load_context: dict[str, Any] | None
    session_context: dict[str, Any] | None
    is_deload: bool | None

@dataclass(frozen=True)
class NormalizedSetFact(RawSetFact):
    e1rm_weight_kg: float | None
    tonnage_kg: float | None
    rir: float | None
    comparison_key: str
    unavailable: tuple[str, ...]
    e1rm_reps: int | None = None

@dataclass(frozen=True)
class ResolvedPeriod:
    start: date
    end: date
    timezone: str
    start_at: datetime
    end_exclusive: datetime
    block_id: int | None

@dataclass(frozen=True)
class AnalyticsQuery:
    period: str = '90'
    start: date | None = None
    end: date | None = None
    block_id: int | None = None
    gym_id: str | None = None
    preview_workout_id: int | None = None
    require_active_preview: bool = False

@dataclass(frozen=True)
class Coverage:
    used: int
    total: int

@dataclass(frozen=True)
class Metric:
    value: float | None
    reason: str | None = None
    coverage: Coverage | None = None

@dataclass(frozen=True)
class PointDetails:
    load_context: dict[str, Any] | None = None
    session_context: dict[str, Any] | None = None
    intensity: Metric | None = None
    effort: Metric | None = None
    coverage: Coverage | None = None
    reasons: tuple[str, ...] = ()
    observation_dates: tuple[date, ...] = ()

@dataclass(frozen=True)
class ExerciseSessionPoint:
    workout_id: int
    exercise_id: int
    at: datetime
    e1rm: float | None
    volume: float | None
    work_sets: int
    drop_sets: int
    is_deload: bool | None
    gym_id: str | None
    source_set_id: int | None
    gym_label: str | None = None
    details: PointDetails | None = None

@dataclass(frozen=True)
class StrengthChange:
    start_value: float | None
    end_value: float | None
    delta_kg: float | None
    delta_pct: float | None
    method: str | None
    workout_ids: tuple[int, ...]

@dataclass(frozen=True)
class EffortSummary:
    mean_rir: Metric
    drift_rir: Metric


class VolumeChannel(TypedDict):
    sets: int
    sets_per_7_days: float
    tonnage: Metric
    tonnage_per_7_days: Metric


class MuscleVolumeChannel(TypedDict):
    direct: float
    indirect: float
    effective: float
    per_7_days: dict[str, float]


@dataclass(frozen=True)
class MuscleExerciseContribution:
    exercise_id: int
    change: StrengthChange | None = None
    observations: tuple[ExerciseSessionPoint, ...] = ()
    coverage: Coverage | None = None
    excluded: tuple[str, ...] = ()

@dataclass(frozen=True)
class MuscleProgress:
    muscle_key: str
    delta_pct: float | None
    exercise_ids: tuple[int, ...]
    excluded: tuple[str, ...]
    contributions: tuple[MuscleExerciseContribution, ...] = ()
    coverage: Coverage | None = None

@dataclass(frozen=True)
class AnalyticsFacts:
    period: ResolvedPeriod | None
    window_sets: list[RawSetFact]
    reference_sets: list[RawSetFact]
    record_sets: list[RawSetFact]
    calendar: list[dict]
    measurements: list[dict]
    blocks: list[dict]
    source_revision: int
    catalog_mappings: list[dict] = field(default_factory=list)
    global_workouts: list[dict] = field(default_factory=list)

    timezone: str = "UTC"
    as_of: datetime | None = None
    preview_start: datetime | None = None
    graph_sets: list[RawSetFact] = field(default_factory=list)
