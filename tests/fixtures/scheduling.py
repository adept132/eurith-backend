from __future__ import annotations

from datetime import date


def carry_over_candidate(**overrides):
    values = {
        "calendar_day_id": 1,
        "target_date": date(2026, 8, 24),
        "exercise_id": 10,
        "muscle": "chest",
        "match_kind": "exact",
        "session_cap_remaining": 4,
        "muscle_mrv_remaining": 8,
        "is_deload": False,
        "readiness_level": "ok",
        "has_pain": False,
        "equipment_available": True,
        "within_microcycle": True,
    }
    values.update(overrides)
    return values
