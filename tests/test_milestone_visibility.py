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
        ceiling_pct=INTERMEDIATE_CAP,
    )
    lifts = [v.milestone.lift for v in result]
    assert len(lifts) == len(set(lifts)), "по каждому движению не больше одной вехи"


def test_taken_milestone_is_hidden_and_the_next_one_shows():
    """Присед 130 при весе 80: взяты 80, 100 и 120 — первая невзятая двойной вес."""
    result = visible_milestones(
        current_e1rm={"squat": 130.0},
        bodyweight=80.0,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    squat = [v for v in result if v.milestone.lift == "squat"]
    assert [v.milestone.code for v in squat] == ["squat_2x_bw"]


def test_unreachable_milestone_is_hidden_entirely():
    """Двойной вес при текущих 60 и потолке 0.5%/нед недостижим в горизонт."""
    result = visible_milestones(
        current_e1rm={"squat": 60.0},
        bodyweight=80.0,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    assert "squat_2x_bw" not in _codes(result)


def test_lift_vanishes_when_even_its_nearest_rung_is_unreachable():
    """Присед 50 кг при весе тела 80: ближайшая ступень — 80 кг, это +60 %
    от текущего. При потолке 0.5 %/нед идти туда 120 недель, горизонт 104 —
    движение пропадает из витрины целиком, а не показывает недостижимое."""
    result = visible_milestones(
        current_e1rm={"squat": 50.0},
        bodyweight=80.0,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    lifts = {v.milestone.lift for v in result}
    assert "squat" not in lifts, "недостижимый присед показывать нельзя"
    assert "bench" in lifts, "остальные движения не должны пострадать"


def test_lift_without_history_shows_its_lowest_rung():
    """Без истории отфильтровать недостижимое нечем — показывается нижняя ступень."""
    result = visible_milestones(
        current_e1rm={},
        bodyweight=80.0,
        ceiling_pct=0.01,
    )
    assert _codes(result) == [
        "squat_bw_5reps", "bench_bw", "deadlift_100kg",
        "ohp_0_5x_bw", "pullup_first", "row_bw_8reps",
    ]
    assert all(v.has_history is False for v in result)
    assert all(v.remaining is None for v in result)


def test_no_history_gives_no_remaining_but_still_shows():
    result = visible_milestones(
        current_e1rm={},
        bodyweight=80.0,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    assert _codes(result), "без истории витрина не пустая"
    assert all(v.remaining is None for v in result)


def test_relative_milestones_vanish_without_bodyweight():
    """Вес тела не заполнен — остаются абсолютные и подтягивания (§7,
    финальное ревью Important 5): подтягивания считаются числом повторов,
    свой вес отягощает автоматически, знать его точно не требуется."""
    result = visible_milestones(
        current_e1rm={},
        bodyweight=None,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    assert set(_codes(result)) == {
        "squat_100kg", "bench_100kg", "deadlift_100kg", "pullup_first",
    }


def test_remaining_is_the_gap_to_the_threshold():
    """Остаток — это разница e1RM (финальное ревью, Critical), не «целевые кг
    минус current». bench_100kg: target_reps=1, target_e1rm = 100 × 31/30 =
    103.3(3) -> округление 103.3; remaining = 103.3 - 87.5 = 15.8. `target`
    на карточке при этом остаётся голыми килограммами штанги (100.0) — это
    решение 5.1 не меняется, меняется только то, ПО ЧЕМУ считается остаток."""
    result = visible_milestones(
        current_e1rm={"bench": 87.5},
        bodyweight=80.0,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    bench = [v for v in result if v.milestone.lift == "bench"][0]
    assert bench.milestone.code == "bench_100kg"
    assert bench.remaining == 15.8
    assert bench.target == 100.0


def test_lowest_unmet_threshold_wins_not_declaration_order():
    """Порядок в каталоге не задаёт лестницу — её задают килограммы."""
    result = visible_milestones(
        current_e1rm={"deadlift": 90.0},
        bodyweight=100.0,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    deadlift = [v for v in result if v.milestone.lift == "deadlift"][0]
    assert deadlift.milestone.code == "deadlift_100kg", "сотка ниже полутора своих (150)"


def test_lift_without_history_still_shows_when_another_lift_has_history():
    """История по приседу не должна прятать остальные пять движений."""
    result = visible_milestones(
        current_e1rm={"squat": 130.0},
        bodyweight=80.0,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    lifts = {v.milestone.lift for v in result}
    assert "bench" in lifts, "жим без истории обязан показаться"
    assert "deadlift" in lifts
    assert len(lifts) == 6, "показываются все шесть движений"


def test_far_but_reachable_milestone_is_shown():
    """Полтора своих веса достижимо за горизонт и обязано показаться.

    current=110 взят выше target_e1rm обеих ступеней ниже (squat_bw_5reps
    93.3, squat_100kg 103.3 при 80 кг) — обе уже взяты по e1RM, а не по
    голым кг (было бы 100 до правки Critical: 100 kg current совпадало бы с
    squat_100kg=100 кг ровно и подменяло бы «взято» дырой в округлении)."""
    result = visible_milestones(
        current_e1rm={"squat": 110.0},
        bodyweight=80.0,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    squat = [v for v in result if v.milestone.lift == "squat"][0]
    assert squat.milestone.code == "squat_1_5x_bw"
    assert squat.target == 120.0
    assert squat.remaining == 14.0


def test_pullup_without_bodyweight_has_no_numbers():
    """Подтягивание без веса тела показывается «слепым»: без target и без
    remaining — так же, как у вех без истории (финальное ревью Important 5)."""
    result = visible_milestones(
        current_e1rm={}, bodyweight=None, ceiling_pct=INTERMEDIATE_CAP,
    )
    pullup = [v for v in result if v.milestone.code == "pullup_first"][0]
    assert pullup.target is None
    assert pullup.remaining is None
    assert pullup.has_history is False


def test_row_bw_8reps_uses_target_e1rm_not_bare_kg():
    """Порог — 101.3 (80 × (1 + 8/30)), а не голые 80 кг штанги: раньше веха
    пряталась уже при e1RM 80 (финальное ревью, Critical)."""
    visible = visible_milestones(
        current_e1rm={"row": 90.0}, bodyweight=80.0, ceiling_pct=INTERMEDIATE_CAP,
    )
    row = [v for v in visible if v.milestone.lift == "row"]
    assert [v.milestone.code for v in row] == ["row_bw_8reps"]
    assert row[0].target == 80.0, "штанга, которую человек будет поднимать, не меняется"
    assert row[0].remaining == 11.3

    hidden = visible_milestones(
        current_e1rm={"row": 105.0}, bodyweight=80.0, ceiling_pct=INTERMEDIATE_CAP,
    )
    assert "row_bw_8reps" not in _codes(hidden), "e1RM 105 уже выше настоящего порога 101.3"


def test_squat_bw_5reps_uses_target_e1rm_not_bare_kg():
    """Порог — 93.3 (80 × (1 + 5/30)); по голым кг веха гасла на 13 кг раньше."""
    result = visible_milestones(
        current_e1rm={"squat": 85.0}, bodyweight=80.0, ceiling_pct=INTERMEDIATE_CAP,
    )
    squat = [v for v in result if v.milestone.lift == "squat"][0]
    assert squat.milestone.code == "squat_bw_5reps"
    assert squat.target == 80.0
    assert squat.remaining == 8.3


def test_pullup_10_reps_is_reachable_and_ranks_after_plus20():
    """pullup_10_reps (e1RM 106.7 при 80 кг) труднее pullup_plus20 (103.3) —
    лестница по e1RM ставит его дальше, а не рядом с pullup_first (80 по
    голым кг, откуда и была мёртвая карточка, финальное ревью Critical)."""
    result = visible_milestones(
        current_e1rm={"pullup": 104.0}, bodyweight=80.0, ceiling_pct=INTERMEDIATE_CAP,
    )
    pullup = [v for v in result if v.milestone.lift == "pullup"][0]
    assert pullup.milestone.code == "pullup_10_reps"
    assert pullup.target == 80.0, "штанга тут ни при чём — это счёт повторов своим весом"
    assert pullup.remaining == 2.7


def test_taken_milestones_are_never_returned():
    """Ни одна показанная веха не может быть уже взятой: остаток строго > 0."""
    result = visible_milestones(
        current_e1rm={"squat": 130.0, "bench": 95.0, "deadlift": 145.0},
        bodyweight=80.0,
        ceiling_pct=INTERMEDIATE_CAP,
    )
    for v in result:
        if v.remaining is not None:
            assert v.remaining > 0, f"{v.milestone.code}: показана взятая веха"
