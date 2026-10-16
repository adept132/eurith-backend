"""Typed, unrounded analytics resources. Readiness is supplied by the route fence."""
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator
from api.services.analytics.types import Coverage, Metric, StrengthChange, EffortSummary, ResolvedPeriod


class DTO(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class AnalyticsScope(DTO):
    period: ResolvedPeriod | None
    timezone: str
    gym_id: str | None = None
    gym_scope: str
    exercise_id: int | None = None
    block_id: int | None = None
    block_ids: list[int] = Field(default_factory=list)
    preview_workout_id: int | None = None
    reason: str | None = None


class AnalyticsEnvelope(DTO):
    rules_version: Literal['p1-08-v1'] = 'p1-08-v1'
    formula_id: Literal['lombardi-v1'] = 'lombardi-v1'
    data_revision: int
    computed_at: datetime
    # None is deliberately unresolved by this read-only composition layer.
    state: Literal['ready', 'rebuilding', 'disabled'] | None = None
    scope: AnalyticsScope


class VolumeChannel(DTO):
    sets: int
    sets_per_7_days: float | None
    tonnage: Metric
    tonnage_per_7_days: Metric


class MuscleVolumeChannel(DTO):
    direct: float
    indirect: float
    effective: float
    per_7_days: dict[str, float | None]


class VolumeSummary(DTO):
    work_sets: int
    drop_sets: int
    work_sets_per_7_days: float | None
    drop_sets_per_7_days: float | None
    tonnage: Metric
    tonnage_per_7_days: Metric
    normal: VolumeChannel
    drop: VolumeChannel
    muscles: dict[str, dict[str, MuscleVolumeChannel]]
    coverage: Coverage
    calendar_days: int | None
    reason: str | None = None


class SetObservation(DTO):
    set_id: int
    exercise_instance_id: int
    set_number: int
    setup_id: str | None
    gym_id: str | None
    gym_label: str | None
    at: datetime
    observation_date: date
    set_type: str
    actual_reps: int | None
    e1rm_reps: int | None
    e1rm_weight_kg: float | None
    tonnage_kg: float | None
    rir: float | None
    intensity: Metric
    load_context: dict[str, Any] | None
    session_context: dict[str, Any] | None
    reasons: tuple[str, ...]


class PointDetails(DTO):
    load_context: dict[str, Any] | None
    session_context: dict[str, Any] | None
    intensity: Metric
    effort: Metric
    coverage: Coverage
    metric_coverage: dict[str, Coverage]
    reasons: tuple[str, ...]
    observation_dates: tuple[date, ...]
    observations: list[SetObservation]


class ExercisePoint(DTO):
    workout_id: int
    exercise_id: int
    at: datetime
    e1rm: float | None
    volume: float | None
    work_sets: int
    drop_sets: int
    is_deload: bool | None
    gym_id: str | None
    gym_label: str | None
    source_set_id: int | None
    details: PointDetails


class ExerciseSummary(DTO):
    best_e1rm: Metric
    strength: StrengthChange
    strength_reason: str | None
    volume: VolumeSummary
    intensity: Metric
    effort: EffortSummary


class RecordEntry(DTO):
    exercise_id: int
    workout_id: int
    exercise_instance_id: int
    set_id: int
    set_number: int
    at: datetime
    weight: float | None
    reps: int | None
    actual_reps: int | None
    gym_id: str | None
    gym_label: str | None
    is_deload: bool | None
    load_context: dict[str, Any] | None
    session_context: dict[str, Any] | None
    e1rm: float | None = None
    previous_e1rm: float | None = None
    value: float | None = None


class RecordBaseline(DTO):
    exercise_id: int
    rules_version: Literal['p1-08-v1'] = 'p1-08-v1'
    formula_id: Literal['lombardi-v1'] = 'lombardi-v1'
    data_revision: int
    gym_scope: str
    as_of: datetime
    excluded_workout_id: int | None
    best_e1rm: float | None
    best: RecordEntry | None
    band: dict[str, RecordEntry]
    weight_at_reps: dict[str, RecordEntry]
    set_volume: RecordEntry | None


class Exclusion(DTO):
    reason: str
    exercise_id: int | None = None
    workout_id: int | None = None
    set_id: int | None = None


class ExerciseAnalytics(AnalyticsEnvelope):
    summary: ExerciseSummary
    points: list[ExercisePoint]
    record_baseline: RecordBaseline
    record_events: list[RecordEntry]
    coverage: dict[str, Coverage]
    excluded: list[Exclusion]


class MuscleContribution(DTO):
    exercise_id: int
    change: StrengthChange | None
    observations: tuple[ExercisePoint, ...]
    coverage: Coverage
    excluded: tuple[str, ...]


class MuscleProgress(DTO):
    muscle_key: str
    delta_pct: float | None
    exercise_ids: tuple[int, ...]
    excluded: tuple[str, ...]
    contributions: tuple[MuscleContribution, ...]
    coverage: Coverage


class MuscleAnalytics(AnalyticsEnvelope):
    muscles: list[MuscleProgress]
    volume: VolumeSummary
    coverage: Coverage
    excluded: list[Exclusion] = Field(default_factory=list)


class BlockMetadata(DTO):
    id: int
    block_index: int
    status: str
    start_date: date
    planned_end_date: date | None
    actual_end_date: date | None
    phases: list[dict[str, Any]] | None


class BodyObservation(DTO):
    id: int | None
    source: str | None
    date: date
    value: float
    date_source: str
    date_estimated: bool


class BodyMetric(DTO):
    before: BodyObservation | None
    after: BodyObservation | None
    delta: float | None


class BodyContext(DTO):
    scope: Literal['block_context'] = 'block_context'
    metrics: dict[str, BodyMetric]
    reason: str | None = None


class Obligation(DTO):
    obligation_uuid: str | None
    origin_date: date
    target_date: date
    block_id: int | None
    completed_on: date | None
    workout_ids: list[int]
    unresolved_workout_ids: list[int]
    transferred: bool
    completed_outside_period: bool


class AdherenceContext(DTO):
    scope: Literal['block_context'] = 'block_context'
    planned_days: int | None = None
    completed_days: int | None = None
    missed_days: int | None = None
    percent: int | None = None
    reason: str | None = None
    obligations: list[Obligation] = Field(default_factory=list)
    unresolved_completion_count: int | None = None
    completion_reason: str | None = None
    transferred_in: list[Obligation] = Field(default_factory=list)
    transferred_in_count: int | None = None
    transferred_out: list[Obligation] = Field(default_factory=list)
    transferred_out_count: int | None = None
    extra_workouts: int | None = None


class BlockAnalytics(AnalyticsEnvelope):
    block: BlockMetadata
    exercises: dict[int, ExerciseAnalytics]
    muscles: list[MuscleProgress]
    volume: VolumeSummary
    intensity: Metric
    effort: EffortSummary
    body: BodyContext
    adherence: AdherenceContext
    excluded: list[Exclusion] = Field(default_factory=list)


class CalendarAnalytics(AnalyticsEnvelope):
    """Closed calendar summary shared with immutable periodic reports."""
    exercises: dict[int, ExerciseAnalytics]
    exercise_names: dict[int, str]
    volume: VolumeSummary
    intensity: Metric
    effort: EffortSummary
    adherence: AdherenceContext
    sessions: int
    total_minutes: int


class Difference(DTO):
    value: float | None
    unit: str
    reason: str | None = None
    scope: Literal['local_load', 'block_context'] = 'local_load'


class ComparisonDifferences(DTO):
    muscles: dict[str, Difference]
    work_sets_per_7_days: Difference
    drop_sets_per_7_days: Difference
    tonnage_per_7_days: Difference
    intensity: Difference
    mean_rir: Difference
    drift_rir: Difference
    adherence: Difference
    body: dict[str, Difference]


class BlockComparison(AnalyticsEnvelope):
    a: BlockAnalytics
    b: BlockAnalytics
    common_exercise_ids: list[int]
    added: list[int]
    removed: list[int]
    excluded: list[Exclusion]
    differences: ComparisonDifferences
    description: str

class AnalyticsUnavailable(DTO):
    """Unresolved resource: deliberately contains no potentially stale metrics."""
    rules_version: Literal['p1-08-v1'] = 'p1-08-v1'
    formula_id: Literal['lombardi-v1'] = 'lombardi-v1'
    data_revision: int
    computed_at: datetime
    state: Literal['rebuilding', 'disabled']
    scope: None = None


class AnalyticsCapabilities(DTO):
    read_encodings: list[Literal['compact-v1']] = Field(default_factory=lambda: ['compact-v1'])
    timezone: str
    write_enabled: bool
    context_schema_version: Literal[1] = 1
    rules_version: Literal['p1-08-v1'] = 'p1-08-v1'
    state: Literal['ready', 'rebuilding', 'disabled']
    data_revision: int



class GymQuery(DTO):
    encoding: Literal['compact-v1'] | None = None
    gym_id: UUID | Literal['unknown'] | None = None

    @property
    def gym_filter(self) -> str | None:
        return str(self.gym_id) if self.gym_id is not None else None


class PeriodQuery(GymQuery):
    period: Literal['7d', '30d', '90d', '365d', 'all', 'custom', 'block'] = '90d'
    start: date | None = None
    end: date | None = None
    block_id: int | None = Field(None, gt=0)
    preview_workout_id: int | None = Field(None, gt=0)

    @model_validator(mode='after')
    def valid_scope(self):
        from api.services.analytics.periods import validate_measurement_bounds
        if self.period == 'custom':
            if self.start is None or self.end is None:
                raise ValueError('invalid_period_bounds')
            validate_measurement_bounds(self.start, self.end)
        elif self.start is not None or self.end is not None:
            raise ValueError('conflicting_period_parameters')
        if (self.period == 'block') != (self.block_id is not None):
            raise ValueError('conflicting_period_parameters')
        return self

    def to_query(self):
        from api.services.analytics.types import AnalyticsQuery
        return AnalyticsQuery(period=self.period.removesuffix('d'), start=self.start, end=self.end,
                              block_id=self.block_id, gym_id=self.gym_filter,
                              preview_workout_id=self.preview_workout_id, require_active_preview=True)


class CompareQuery(GymQuery):
    block_a_id: int = Field(gt=0)
    block_b_id: int = Field(gt=0)
