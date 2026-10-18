from types import SimpleNamespace

from api.services.scheduling_engine import SchedulingEngine


def _plan(plan_id: int, *, day: str = "upper", archived: bool = False):
    return SimpleNamespace(
        id=plan_id,
        day_tag=day,
        meso_tag="adaptive",
        micro_tag="adaptive",
        is_archived=archived,
    )


def test_equal_score_prefers_latest_plan_version():
    selected = SchedulingEngine._score_and_find_best_plan(
        plans=[_plan(10), _plan(22)],
        target_day_name="Upper",
        meso_tag="medium",
        micro_tag="hard",
    )

    assert selected == 22


def test_plan_selection_never_crosses_split_days():
    selected = SchedulingEngine._score_and_find_best_plan(
        plans=[_plan(30, day="lower"), _plan(20, day="upper")],
        target_day_name="Upper",
        meso_tag="medium",
        micro_tag="hard",
    )

    assert selected == 20


def test_plan_selection_never_revives_an_archived_version():
    selected = SchedulingEngine._score_and_find_best_plan(
        plans=[_plan(30, archived=True), _plan(20)],
        target_day_name="Upper",
        meso_tag="medium",
        micro_tag="hard",
    )

    assert selected == 20
