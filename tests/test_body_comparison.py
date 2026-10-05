from datetime import date
from types import SimpleNamespace

from api.services.body_comparison import nearest_measurement, intersect_phases


def point(day, value, fields=None):
    return SimpleNamespace(measured_on=day, recorded_at=None, weight=value, submitted_fields=fields)


def test_nearest_measurement_prefers_earlier_and_excludes_carried_values():
    rows = [point(date(2026, 9, 8), 80, ["height"]),
            point(date(2026, 9, 9), 79, ["weight"]),
            point(date(2026, 9, 11), 78, ["weight"])]
    assert nearest_measurement(rows, "weight", date(2026, 9, 10))["value"] == 79
    assert nearest_measurement(rows, "weight", date(2026, 9, 19)) is None
    assert nearest_measurement([point(date(2026, 9, 10), 80, None)], "weight", date(2026, 9, 10)) is None


def test_intersect_phases_respects_early_close_and_inserted_deload():
    block = SimpleNamespace(
        id=1, start_date=date(2026, 9, 1), planned_end_date=date(2026, 9, 30),
        actual_end_date=date(2026, 9, 16),
        phases=[{"phase_number": 1, "name": "Работа", "effort_tier": "hard", "length_days": 10},
                {"phase_number": 3, "name": "Разгрузка", "effort_tier": "deload", "length_days": 7},
                {"phase_number": 2, "name": "Сила", "effort_tier": "hard", "length_days": 10}],
    )
    result = intersect_phases([block], date(2026, 9, 8), date(2026, 9, 20))
    assert [(p["phase_number"], p["start_date"], p["end_date"]) for p in result] == [
        (1, date(2026, 9, 8), date(2026, 9, 10)),
        (3, date(2026, 9, 11), date(2026, 9, 16)),
    ]
