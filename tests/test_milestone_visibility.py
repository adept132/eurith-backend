"""Правило видимости вех (P1-03 ч.2, §5.2)."""
from api.services.milestones.visibility import visible_milestones

# Потолок недельного прироста e1RM, доля от текущего (WEEKLY_GROWTH_CAP_PCT).
INTERMEDIATE_CAP = 0.005


def _codes(result):
    return [v.milestone.code for v in result]


def test_one_milestone_per_lift_at_most():
    result = visible_milestones(
        current_e1rm={"squat": 100.0, "bench": 80.0},
        bodyweight=80.0,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    lifts = [v.milestone.lift for v in result]
    assert len(lifts) == len(set(lifts)), "по каждому движению не больше одной вехи"


def test_taken_milestone_is_hidden_and_the_next_one_shows():
    """Присед 130 при весе 80: сотка и «свой вес на пять» взяты, полтора — нет."""
    result = visible_milestones(
        current_e1rm={"squat": 130.0},
        bodyweight=80.0,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    assert _codes(result) == ["squat_1_5x_bw"]


def test_unreachable_milestone_is_hidden_entirely():
    """Двойной вес при текущих 60 и потолке 0.5%/нед недостижим в горизонт."""
    result = visible_milestones(
        current_e1rm={"squat": 60.0},
        bodyweight=80.0,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    assert "squat_2x_bw" not in _codes(result)


def test_beginner_sees_only_the_lowest_rung_of_each_lift():
    """Без истории отфильтровать недостижимое нечем — решение 10."""
    result = visible_milestones(
        current_e1rm={},
        bodyweight=80.0,
        experience_level="beginner",
        ceiling_pct=0.01,
    )
    assert _codes(result) == [
        "squat_100kg", "bench_bw", "deadlift_100kg",
        "ohp_0_5x_bw", "pullup_first", "row_bw_8reps",
    ]
    assert all(v.has_history is False for v in result)
    assert all(v.remaining is None for v in result)


def test_no_history_gives_no_remaining_but_still_shows():
    result = visible_milestones(
        current_e1rm={},
        bodyweight=80.0,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    assert _codes(result), "без истории витрина не пустая"
    assert all(v.remaining is None for v in result)


def test_relative_milestones_vanish_without_bodyweight():
    """Вес тела не заполнен — остаются только абсолютные (§7)."""
    result = visible_milestones(
        current_e1rm={},
        bodyweight=None,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    assert set(_codes(result)) <= {"squat_100kg", "deadlift_100kg"}


def test_remaining_is_the_gap_to_the_threshold():
    result = visible_milestones(
        current_e1rm={"bench": 87.5},
        bodyweight=80.0,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    bench = [v for v in result if v.milestone.lift == "bench"][0]
    assert bench.milestone.code == "bench_100kg"
    assert bench.remaining == 12.5
    assert bench.target == 100.0


def test_lowest_unmet_threshold_wins_not_declaration_order():
    """Порядок в каталоге не задаёт лестницу — её задают килограммы."""
    result = visible_milestones(
        current_e1rm={"deadlift": 90.0},
        bodyweight=100.0,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    deadlift = [v for v in result if v.milestone.lift == "deadlift"][0]
    assert deadlift.milestone.code == "deadlift_100kg", "сотка ниже полутора своих (150)"


def test_lift_without_history_still_shows_when_another_lift_has_history():
    """История по приседу не должна прятать остальные пять движений."""
    result = visible_milestones(
        current_e1rm={"squat": 130.0},
        bodyweight=80.0,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    lifts = {v.milestone.lift for v in result}
    assert "bench" in lifts, "жим без истории обязан показаться"
    assert "deadlift" in lifts
    assert len(lifts) == 6, "показываются все шесть движений"


def test_far_but_reachable_milestone_is_shown():
    """Веха на 50% выше текущего достижима за горизонт и обязана показаться."""
    result = visible_milestones(
        current_e1rm={"squat": 100.0},
        bodyweight=80.0,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    squat = [v for v in result if v.milestone.lift == "squat"][0]
    assert squat.milestone.code == "squat_1_5x_bw"
    assert squat.target == 120.0


def test_taken_milestones_are_never_returned():
    """Ни одна показанная веха не может быть уже взятой: остаток строго > 0."""
    result = visible_milestones(
        current_e1rm={"squat": 130.0, "bench": 95.0, "deadlift": 145.0},
        bodyweight=80.0,
        experience_level="intermediate",
        ceiling_pct=INTERMEDIATE_CAP,
    )
    for v in result:
        if v.remaining is not None:
            assert v.remaining > 0, f"{v.milestone.code}: показана взятая веха"
