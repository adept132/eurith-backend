"""Материализация снапшотов отчётов.

Вызывается из двух мест: роутера /reports и фоновой проекции доменных
событий. Идемпотентна по (пользователь, тип, начало периода) — воркер и
клиент могут разойтись в оценке текущей даты на границе суток, и это
сдвигает лишь МОМЕНТ создания, но не содержимое и не количество записей.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AppUserProfile, PeriodizationProposal, PeriodReport
from api.services.progression.params import DEFAULT_RIR
from api.services.reports.metrics import ReportMetrics, compute_metrics, from_analytics, has_activity
from api.services.analytics.config import analytics_enabled
from api.services.analytics.publication import coherent_read
from api.services.analytics.service import calendar_analytics
from api.services.reports.periods import PERIOD_TYPES, closed_periods
from api.services.reports.rules import RULES_VERSION, Action, RuleContext, build_actions

REPORT_SHAPE_VERSION = 3
LEGACY_RULES_VERSION = 2
LEGACY_SHAPE_VERSION = 2

# Only registered stored pairs identify a calculation. Never infer from today's gate.
CALCULATION_LABELS = {
    (1, 1): 'Расчёт по прежним правилам (Бржицки)',
    (2, 2): 'Расчёт по прежним правилам (Бржицки)',
    (3, 3): 'Единая аналитика: Lombardi',
}


def calculation_label(rules_version: int, shape_version: int) -> str:
    return CALCULATION_LABELS.get((rules_version, shape_version), 'Создано до обновления аналитики')

PERIOD_TITLE = {"week": "неделю", "month": "месяц", "year": "год"}


def build_payload(metrics: ReportMetrics, actions: list[Action], *,
                  analytics_meta: dict | None = None) -> dict:
    """JSON-представление снапшота. Даты — ISO-строки: payload переживает
    и БД, и офлайн-кэш клиента."""
    data = asdict(metrics)
    data["period_start"] = metrics.period_start.isoformat()
    data["period_end"] = metrics.period_end.isoformat()
    for record in data["records"]:
        record["achieved_on"] = record["achieved_on"].isoformat()
    payload = {
        "shape_version": REPORT_SHAPE_VERSION if analytics_meta else LEGACY_SHAPE_VERSION,
        "rules_version": RULES_VERSION if analytics_meta else LEGACY_RULES_VERSION,
        "metrics": data,
        "actions": [asdict(action) for action in actions],
    }
    if analytics_meta is not None:
        payload['analytics_meta'] = analytics_meta
    return payload


async def _rule_context(session: AsyncSession, app_user_id: int) -> RuleContext:
    proposal = (await session.execute(
        select(PeriodizationProposal)
        .where(
            PeriodizationProposal.app_user_id == app_user_id,
            PeriodizationProposal.status == "pending",
        )
        .order_by(PeriodizationProposal.id.desc())
    )).scalars().first()
    return RuleContext(
        pending_proposal_id=proposal.id if proposal else None,
        pending_proposal_kind=proposal.kind if proposal else None,
        target_rir=DEFAULT_RIR,
    )


async def ensure_reports(
    session: AsyncSession, app_user_id: int, local_date: date
) -> int:
    """Создать недостающие отчёты за закрытые периоды. Возвращает счётчик."""
    from api.services.notification_service import create_notification

    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalar_one_or_none()
    level = profile.experience_level if profile else None
    context = await _rule_context(session, app_user_id)

    created = 0
    for period_type in PERIOD_TYPES:
        for start, end in closed_periods(period_type, local_date):
            exists = (await session.execute(
                select(PeriodReport.id).where(
                    PeriodReport.app_user_id == app_user_id,
                    PeriodReport.period_type == period_type,
                    PeriodReport.period_start == start,
                )
            )).scalar_one_or_none()
            if exists is not None:
                continue

            analytics_meta = None
            if analytics_enabled():
                async def assemble():
                    return await calendar_analytics(session, app_user_id, start, end)

                resource = await coherent_read(session, app_user_id, assemble)
                if resource.state != 'ready':
                    continue
                metrics = from_analytics(resource, period_type, level, resource.exercise_names)
                analytics_meta = dict(rules_version=resource.rules_version,
                    formula_id=resource.formula_id, data_revision=resource.data_revision)
            else:
                metrics = await compute_metrics(
                    session, app_user_id, period_type, start, end, level
                )
            if not has_activity(metrics):
                continue

            actions = build_actions(metrics, context)
            payload = build_payload(metrics, actions, analytics_meta=analytics_meta)
            statement = (
                insert(PeriodReport)
                .values(
                    app_user_id=app_user_id,
                    period_type=period_type,
                    period_start=start,
                    period_end=end,
                    payload=payload,
                    rules_version=payload['rules_version'],
                    shape_version=payload['shape_version'],
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        PeriodReport.app_user_id,
                        PeriodReport.period_type,
                        PeriodReport.period_start,
                    ]
                )
                .returning(PeriodReport.id)
            )
            report_id = (await session.execute(statement)).scalar_one_or_none()
            if report_id is None:
                # Другой процесс успел первым — уведомление он же и создал.
                continue

            created += 1
            await create_notification(
                session,
                app_user_id=app_user_id,
                event_type="period_report",
                entity_type="period_report",
                entity_id=report_id,
                title=f"Отчёт за {PERIOD_TITLE[period_type]} готов",
                body="Посмотрите итоги периода и что с ними делать.",
                payload={"route": f"/reports/{period_type}/{start.isoformat()}"},
                dedupe_key=f"period_report:{period_type}:{start.isoformat()}",
            )
    return created
