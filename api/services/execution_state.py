"""Merge execution metadata only; synthetic forecast results are never persisted."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def merge_mode(current: dict | None, incoming: dict | None) -> dict | None:
    if not current:
        return incoming
    if not incoming:
        return current
    result = dict(incoming if _time(incoming['changed_at']) > _time(current['changed_at']) else current)
    dismissed = [s for s in (current.get('banner_dismissed_at'), incoming.get('banner_dismissed_at')) if s]
    result['banner_dismissed_at'] = max(dismissed, key=_time) if dismissed else None
    return result


def merge_exercise(current: dict | None, incoming: dict | None, exercise_id: int) -> dict | None:
    a = current if current and current.get('exercise_id') == exercise_id else None
    b = incoming if incoming and incoming.get('exercise_id') == exercise_id else None
    if not a and not b:
        return None
    slots = dict((a or {}).get('slots', {}))
    for key, value in (b or {}).get('slots', {}).items():
        old = slots.get(key)
        if not old or _time(value['updated_at']) > _time(old['updated_at']) or (
            _time(value['updated_at']) == _time(old['updated_at']) and value['state'] == 'draft'
        ):
            slots[key] = value
    return dict(schema_version=1, exercise_id=exercise_id, slots=slots)


def retire_slots(current: dict | None, deleted_uuids: set[str]) -> dict | None:
    if not current:
        return current
    slots = dict(current.get('slots', {}))
    changed = False
    for number, slot in slots.items():
        if slot.get('set_client_uuid') not in deleted_uuids:
            continue
        # JS clients retain millisecond precision; advance a whole millisecond.
        at = max(datetime.now(timezone.utc), _time(slot['updated_at']) + timedelta(milliseconds=1))
        slots[number] = dict(state='forecast', set_client_uuid=None, updated_at=at.isoformat())
        changed = True
    return dict(current, slots=slots) if changed else current


def prescribed_working_progress(exercise) -> tuple[int | None, int]:
    """Mandatory slots only; capacity/statistics continue to count actual volume."""
    prescription = exercise.prescription or {}
    owner = (prescription.get('basis') or {}).get('exercise_id')
    if owner is not None and owner != exercise.exercise_id:
        return None, 0
    count = len(prescription.get('sets') or []) or exercise.target_sets
    target = count if isinstance(count, int) and count > 0 else None
    roots = sorted((row for row in exercise.sets if row.set_type == 'normal' and row.parent_set_id is None
                    and not ((row.shown_target_snapshot or {}).get('execution') or {}).get('extra')),
                   key=lambda row: (row.set_number, row.id))
    state = exercise.execution_state or {}
    slots = state.get('slots') if state.get('exercise_id') == exercise.exercise_id else None
    if not slots or target is None:
        return target, sum(bool(row.is_completed) for row in roots)
    by_uuid = {str(row.client_uuid): row for row in roots}
    bound = {slot.get('set_client_uuid') for slot in slots.values()} & by_uuid.keys()
    unbound = iter(row for row in roots if str(row.client_uuid) not in bound)
    completed = 0
    for number in range(1, target + 1):
        uuid = (slots.get(str(number)) or {}).get('set_client_uuid')
        row = by_uuid.get(uuid) if uuid in by_uuid else next(unbound, None)
        completed += bool(row and row.is_completed)
    return target, completed
