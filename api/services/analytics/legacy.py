"""Validate persisted legacy evidence only; never consult current equipment settings."""
from uuid import UUID
from pydantic import ValidationError
from api.schemas.warmups import WarmupLoadContext
from .policy import finite_number


def recorded_load_context(*, analytics_snapshot, saved_snapshot, mode, setup_id, defaults):
    """Read-only compatibility for scalar rows with no resistance evidence.

    This describes the recorded number, never a reconstructed apparatus, full
    resistance, body mass, implement count or plates-only conversion. Explicit
    unknown/malformed snapshots must retain their original missingness.
    """
    if any(value is not None for value in
           (analytics_snapshot, saved_snapshot, mode, setup_id, defaults)):
        return None
    return dict(schema_version=1, model='recorded_load', input_basis='recorded',
                source='legacy_recorded_weight')


def legacy_load_context(snapshot: dict | None, *, mode: str | None,
                        gym_id=None, setup_id=None) -> dict | None:
    if not isinstance(snapshot, dict):
        return None
    if 'display_unit' in snapshot and snapshot['display_unit'] not in ('kg', 'lbs'):
        return None
    def identity(value):
        return UUID(str(value)) if value is not None else None
    try:
        gym, setup = identity(gym_id), identity(setup_id)
        equipment = (WarmupLoadContext.model_validate(snapshot['equipment'])
                     if 'equipment' in snapshot else None)
        if equipment is not None and (equipment.load_mode != mode
                or equipment.gym_profile_id != gym or equipment.setup_id != setup):
            return None
        if mode is None:
            if (equipment is None or equipment.kind != 'barbell' or setup is not None
                    or equipment.weight_basis != 'including_start_weight'):
                return None
            return dict(schema_version=1, model='barbell', input_basis='full',
                        source='legacy_saved_equipment_snapshot')
        if mode not in {'stack', 'plate_loaded'} or snapshot.get('mode') != mode:
            return None
        if ('gym_id' not in snapshot or 'setup_id' not in snapshot
                or identity(snapshot['gym_id']) != gym or identity(snapshot['setup_id']) != setup):
            return None
        sides, basis = snapshot.get('loading_sides'), snapshot.get('weight_basis')
        if type(sides) is not int or sides not in (1, 2):
            return None
        if basis not in ({'displayed'} if mode == 'stack' else {'plates_only', 'including_start_weight'}):
            return None
        base = snapshot.get('base_weight')
        if base is not None and (finite_number(base) is None or finite_number(base) < 0):
            return None
        step = finite_number(snapshot.get('step_value'))
        if step is None or step <= 0 or snapshot.get('step_unit') not in {'kg', 'lb'}:
            return None
        if equipment is not None:
            allowed = {'plate_loaded', 'manual'} if mode == 'plate_loaded' else {'step', 'manual'}
            if (equipment.kind not in allowed or equipment.loading_sides != sides
                    or equipment.weight_basis != basis or equipment.base_weight_kg != base
                    or (equipment.step is not None and (equipment.step.value != step
                        or equipment.step.unit != snapshot['step_unit']))):
                return None
        return dict(schema_version=1, model=mode,
                    input_basis={'including_start_weight':'full'}.get(basis, basis),
                    base_weight_kg=base, source='legacy_saved_machine_snapshot')
    except (ValueError, TypeError, ValidationError):
        return None
