"""Confirmable anomaly signals; storage validation is a separate contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from statistics import median
from api.schemas.analytics_context import AnomalyReview
from api.services.analytics.types import NormalizedSetFact
from api.services.analytics.load import lombardi

# Large-load review signal; user confirmation keeps the record eligible.
UNUSUAL_WEIGHT_KG = 600.0


# --- Относительные пороги [КОНФИГ]: ловят опечатки вида 100 -> 1000. ---
E1RM_JUMP_FACTOR = 1.5
WEIGHT_MEDIAN_FACTOR = 3.0
MIN_HISTORY_SESSIONS = 3
DERIVED_REASON_CODES = frozenset({'unusual_weight', 'e1rm_jump', 'weight_median_jump', 'legacy_unknown'})


def review_input_state(value):
    """Only user metadata participates in correction/replay comparisons."""
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    value = value or {}
    return (tuple(code for code in value.get('reason_codes', []) if code not in DERIVED_REASON_CODES),
            bool(value.get('confirmed')))

# Типы подходов, которые не участвуют в аналитике, — относительные правила
# к ним не применяем, чтобы не дёргать пользователя на разминке.
_NON_WORKING_SET_TYPES = frozenset({"warmup", "warmup_effort"})

LEVEL_OK = "ok"
LEVEL_SUSPICIOUS = "suspicious"
LEVEL_ABSURD = "absurd"


@dataclass(frozen=True)
class ExerciseStats:
    """Срез истории пользователя по конкретному упражнению за окно наблюдения.

    Считается только по рабочим завершённым подходам, уже помеченные аномалии
    исключаются — иначе одна ошибка расширила бы коридор для следующих.
    """
    sessions: int
    best_e1rm: Optional[float]
    median_weight_kg: Optional[float]


@dataclass(frozen=True)
class AnomalyVerdict:
    level: str
    reason: Optional[str]


def _e1rm(weight_kg: float, reps: int) -> float:
    """Compatibility scalar Lombardi, without RIR."""
    return weight_kg * reps ** 0.1


def check_set(
    weight_kg: Optional[float],
    reps: Optional[int],
    set_type: str,
    stats: Optional[ExerciseStats],
) -> AnomalyVerdict:
    """Оценивает подход. Вес — всегда в килограммах."""
    if weight_kg is None and reps is None:
        return AnomalyVerdict(LEVEL_OK, None)

    w = float(weight_kg or 0.0)
    r = int(reps or 0)

    if w > UNUSUAL_WEIGHT_KG:
        return AnomalyVerdict(LEVEL_SUSPICIOUS, f"Вес {w:.0f} кг требует подтверждения: выше {UNUSUAL_WEIGHT_KG:.0f} кг")

    if set_type in _NON_WORKING_SET_TYPES:
        return AnomalyVerdict(LEVEL_OK, None)

    if stats is None or stats.sessions < MIN_HISTORY_SESSIONS:
        return AnomalyVerdict(LEVEL_OK, None)

    if stats.best_e1rm and r > 0 and w > 0:
        if _e1rm(w, r) > stats.best_e1rm * E1RM_JUMP_FACTOR:
            return AnomalyVerdict(
                LEVEL_SUSPICIOUS,
                "Результат заметно выше твоего лучшего в этом упражнении",
            )

    if stats.median_weight_kg and w > stats.median_weight_kg * WEIGHT_MEDIAN_FACTOR:
        return AnomalyVerdict(
            LEVEL_SUSPICIOUS,
            "Вес заметно выше твоего обычного в этом упражнении",
        )

    return AnomalyVerdict(LEVEL_OK, None)


def resolve_is_anomalous(verdict: AnomalyVerdict, confirmed: bool) -> bool:
    """Итоговое решение для колонки is_anomalous.

    Подтверждение пользователя снимает только "suspicious": если человек сказал
    «да, 250 кг верно», исключать подход из аналитики было бы неуважением к
    его вводу. Абсурдные значения подтверждению не поддаются.
    """
    if verdict.level == LEVEL_ABSURD:
        return True
    if verdict.level == LEVEL_SUSPICIOUS:
        return not confirmed
    return False


def check_analytics_set(fact: NormalizedSetFact, history: list[NormalizedSetFact]) -> AnomalyReview:
    """Review explicit comparable loads, using per-implement Lombardi repetitions."""
    reasons = []
    weight = fact.e1rm_weight_kg
    if weight is not None and weight > UNUSUAL_WEIGHT_KG:
        reasons.append('unusual_weight')
    comparable = [row for row in history if row.exercise_id == fact.exercise_id
        and row.comparison_key == fact.comparison_key and row.workout_id != fact.workout_id
        and row.started_at < fact.started_at and row.is_completed and not row.is_anomalous
        and row.is_deload is not True and row.set_type == 'normal'
        and row.e1rm_weight_kg is not None and row.e1rm_reps]
    if fact.set_type == 'normal' and len({row.workout_id for row in comparable}) >= MIN_HISTORY_SESSIONS:
        best = max(lombardi(row.e1rm_weight_kg, row.e1rm_reps) or 0 for row in comparable)
        current = lombardi(weight, fact.e1rm_reps)
        if current is not None and current > best * E1RM_JUMP_FACTOR:
            reasons.append('e1rm_jump')
        if weight is not None and weight > median(row.e1rm_weight_kg for row in comparable) * WEIGHT_MEDIAN_FACTOR:
            reasons.append('weight_median_jump')
    return AnomalyReview(rules_version='p1-08-v1', reason_codes=reasons, confirmed=False)
