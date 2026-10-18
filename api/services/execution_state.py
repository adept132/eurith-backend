"""Merge execution metadata only; synthetic forecast results are never persisted."""
from __future__ import annotations
from datetime import datetime


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
