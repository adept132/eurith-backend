"""Normal/drop volume channels from normalized facts and catalog mappings."""
from api.services.volume.measure import MuscleContribution, accumulate, contribution

from .policy import eligible, finite_number
from .types import Coverage, Metric, MuscleVolumeChannel, NormalizedSetFact, VolumeChannel


def _tonnage(rows: list[NormalizedSetFact], scale: float = 1.) -> Metric:
    values = [value for row in rows
              if (value := finite_number(row.tonnage_kg)) is not None]
    # Empty channels are known zero; wholly unavailable loads are unknown.
    known = bool(values) or not rows
    return Metric(sum(values) * scale if known else None,
                  None if known else 'unknown_tonnage', Coverage(len(values), len(rows)))


def _channel(rows: list[NormalizedSetFact], scale: float) -> VolumeChannel:
    return VolumeChannel(sets=len(rows), sets_per_7_days=len(rows) * scale,
                         tonnage=_tonnage(rows), tonnage_per_7_days=_tonnage(rows, scale))


def _muscle_channel(value: MuscleContribution, scale: float) -> MuscleVolumeChannel:
    return MuscleVolumeChannel(direct=value.direct, indirect=value.indirect,
        effective=value.effective, per_7_days=dict(direct=value.direct * scale,
        indirect=value.indirect * scale, effective=value.effective * scale))


def volume_summary(rows: list[NormalizedSetFact], muscles_by_exercise: dict[int, dict],
                   *, calendar_days: int) -> dict:
    """Calendar duration includes rest days; mapping coverage counts admitted sets.

    Each tonnage Metric has its own known-load coverage. Muscle channels use
    the existing shared contribution policy, including its catalog de-duplication.
    """
    if isinstance(calendar_days, bool) or not isinstance(calendar_days, int) or calendar_days <= 0:
        raise ValueError('calendar_days must be a positive integer')
    scale = 7 / calendar_days
    normal = [row for row in rows if eligible(row, 'work_sets')]
    drops = [row for row in rows if eligible(row, 'drop_sets')]
    admitted = normal + drops
    channels = {'normal': {}, 'drop': {}}
    mapped = 0
    for row in admitted:
        mapping = muscles_by_exercise.get(row.exercise_id) or {}
        part = contribution(mapping.get('main_muscle_group'),
                            mapping.get('secondary_muscle_groups'), 1)
        if part:
            mapped += 1
        name = 'normal' if row.set_type == 'normal' else 'drop'
        channels[name] = accumulate(channels[name], part)
    zero = MuscleContribution(0., 0.)
    muscles = {key: {name: _muscle_channel(values.get(key, zero), scale)
                     for name, values in channels.items()}
               for key in sorted(channels['normal'].keys() | channels['drop'].keys())}
    return dict(work_sets=len(normal), drop_sets=len(drops),
        work_sets_per_7_days=len(normal) * scale, drop_sets_per_7_days=len(drops) * scale,
        tonnage=_tonnage(admitted), tonnage_per_7_days=_tonnage(admitted, scale),
        normal=_channel(normal, scale), drop=_channel(drops, scale), muscles=muscles,
        coverage=Coverage(mapped, len(admitted)), calendar_days=calendar_days)
