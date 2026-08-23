"""Persist source-aware normalized exercise import aliases.

Revision ID: 20260822_09
Revises: 20260822_06
Create Date: 2026-08-23
"""

import unicodedata
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260822_09"
down_revision: Union[str, Sequence[str], None] = "20260822_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def normalize_external_name(external_name: str) -> str:
    """Migration-local twin of import_service.normalize_external_name."""
    compatible = unicodedata.normalize("NFKC", external_name)
    collapsed = " ".join(compatible.split())
    return unicodedata.normalize("NFKC", collapsed.casefold())


def upgrade() -> None:
    op.add_column(
        "exercise_import_aliases",
        sa.Column("normalized_external_name", sa.String(length=200), nullable=True),
    )

    bind = op.get_bind()
    # Keep the table stable across Python normalization, collision cleanup,
    # and unique-constraint creation. PostgreSQL holds this lock to commit.
    bind.execute(
        sa.text(
            "LOCK TABLE exercise_import_aliases IN SHARE ROW EXCLUSIVE MODE"
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE exercise_import_aliases
            SET source = 'strong'
            WHERE source IS NULL
               OR btrim(source) = ''
               OR source NOT IN ('strong', 'hevy', 'fitbod', 'table', 'notes')
            """
        )
    )

    rows = bind.execute(
        sa.text(
            """
            SELECT id, external_name
            FROM exercise_import_aliases
            ORDER BY id
            """
        )
    ).mappings().all()
    normalized_rows = [
        {
            "row_id": row["id"],
            "normalized_name": normalize_external_name(row["external_name"]),
        }
        for row in rows
    ]
    if normalized_rows:
        bind.execute(
            sa.text(
                """
                UPDATE exercise_import_aliases
                SET normalized_external_name = :normalized_name
                WHERE id = :row_id
                """
            ),
            normalized_rows,
        )

    # A concurrent legacy select-then-insert could have left equivalent rows.
    # Keep the greatest id (the latest write) for each new identity.
    bind.execute(
        sa.text(
            """
            DELETE FROM exercise_import_aliases AS alias
            USING (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY app_user_id, source, normalized_external_name
                            ORDER BY id DESC
                        ) AS collision_rank
                    FROM exercise_import_aliases
                ) AS ranked_aliases
                WHERE collision_rank > 1
            ) AS duplicates
            WHERE alias.id = duplicates.id
            """
        )
    )

    op.alter_column(
        "exercise_import_aliases",
        "normalized_external_name",
        existing_type=sa.String(length=200),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_exercise_import_alias_source",
        "exercise_import_aliases",
        "source IN ('strong', 'hevy', 'fitbod', 'table', 'notes')",
    )
    op.create_unique_constraint(
        "uq_exercise_import_alias_user_source_name",
        "exercise_import_aliases",
        ["app_user_id", "source", "normalized_external_name"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_exercise_import_alias_user_source_name",
        "exercise_import_aliases",
        type_="unique",
    )
    op.drop_constraint(
        "ck_exercise_import_alias_source",
        "exercise_import_aliases",
        type_="check",
    )
    op.drop_column("exercise_import_aliases", "normalized_external_name")
