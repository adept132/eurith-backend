"""Persist immutable source exercise display context for carry-over previews.

Revision ID: 20261018_p113_display
Revises: 20261018_p113_carry
Create Date: 2026-08-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261018_p113_display"
down_revision: Union[str, Sequence[str], None] = "20261018_p113_carry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workout_carry_overs",
        sa.Column("source_exercise_name", sa.String(length=255), nullable=True),
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE workout_carry_overs AS carry_over
            SET source_exercise_name = BTRIM(exercises.name)
            FROM workout_session_exercises AS source_session_exercise
            JOIN exercises ON exercises.id = source_session_exercise.exercise_id
            WHERE source_session_exercise.id = carry_over.source_exercise_id
            """
        )
    )

    missing_placement_context_count = bind.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM workout_carry_overs AS carry_over
            CROSS JOIN LATERAL jsonb_array_elements(carry_over.placements) AS placement(value)
            LEFT JOIN exercises
                ON exercises.id = NULLIF(placement.value ->> 'exercise_id', '')::integer
            LEFT JOIN user_calendar AS calendar_day
                ON calendar_day.id = NULLIF(placement.value ->> 'calendar_day_id', '')::integer
            LEFT JOIN workout_plans AS target_plan
                ON target_plan.id = calendar_day.plan_id
            WHERE exercises.id IS NULL
               OR NULLIF(BTRIM(exercises.name), '') IS NULL
               OR length(BTRIM(exercises.name)) > 255
               OR calendar_day.id IS NULL
            """
        )
    )
    if missing_placement_context_count:
        raise RuntimeError(
            "cannot enforce carry-over placement display context: "
            f"{missing_placement_context_count} placement snapshots could not be resolved"
        )

    bind.execute(
        sa.text(
            """
            WITH rebuilt_placements AS (
                SELECT
                    carry_over.id,
                    jsonb_agg(
                        jsonb_set(
                            jsonb_set(
                                placement.value,
                                '{target_exercise_name}',
                                to_jsonb(BTRIM(exercises.name)),
                                true
                            ),
                            '{target_day_name}',
                            to_jsonb(
                                COALESCE(
                                    NULLIF(BTRIM(target_plan.name), ''),
                                    NULLIF(BTRIM(calendar_day.day_tag), ''),
                                    'Тренировка ' || GREATEST(
                                        COALESCE(calendar_day.microcycle_day_number, 1),
                                        1
                                    )::text
                                )
                            ),
                            true
                        )
                        ORDER BY placement.ordinality
                    ) AS placements
                FROM workout_carry_overs AS carry_over
                CROSS JOIN LATERAL jsonb_array_elements(carry_over.placements)
                    WITH ORDINALITY AS placement(value, ordinality)
                JOIN exercises
                    ON exercises.id = (placement.value ->> 'exercise_id')::integer
                JOIN user_calendar AS calendar_day
                    ON calendar_day.id = (placement.value ->> 'calendar_day_id')::integer
                LEFT JOIN workout_plans AS target_plan
                    ON target_plan.id = calendar_day.plan_id
                GROUP BY carry_over.id
            )
            UPDATE workout_carry_overs AS carry_over
            SET placements = rebuilt_placements.placements
            FROM rebuilt_placements
            WHERE carry_over.id = rebuilt_placements.id
            """
        )
    )

    missing_display_snapshot_count = bind.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM workout_carry_overs AS carry_over
            CROSS JOIN LATERAL jsonb_array_elements(carry_over.placements) AS placement(value)
            WHERE NULLIF(BTRIM(placement.value ->> 'target_exercise_name'), '') IS NULL
               OR NULLIF(BTRIM(placement.value ->> 'target_day_name'), '') IS NULL
               OR length(BTRIM(placement.value ->> 'target_exercise_name')) > 255
               OR length(BTRIM(placement.value ->> 'target_day_name')) > 255
            """
        )
    )
    if missing_display_snapshot_count:
        raise RuntimeError(
            "cannot enforce carry-over placement display context: "
            f"{missing_display_snapshot_count} placement snapshots are incomplete"
        )

    missing_snapshot_count = bind.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM workout_carry_overs
            WHERE NULLIF(BTRIM(source_exercise_name), '') IS NULL
               OR length(BTRIM(source_exercise_name)) > 255
            """
        )
    )
    if missing_snapshot_count:
        raise RuntimeError(
            "cannot enforce carry-over source display context: "
            f"{missing_snapshot_count} rows could not be backfilled"
        )
    op.alter_column(
        "workout_carry_overs",
        "source_exercise_name",
        existing_type=sa.String(length=255),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("workout_carry_overs", "source_exercise_name")
