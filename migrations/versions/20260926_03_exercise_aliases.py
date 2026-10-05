"""Persist source-aware normalized exercise import aliases.

Revision ID: 20260926_03
Revises: 20260926_02
Create Date: 2026-08-23
"""

import hashlib
import unicodedata
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260926_03"
down_revision: Union[str, Sequence[str], None] = "20260926_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NORMALIZED_EXTERNAL_NAME_MAX_CHARS = 3_600


def normalize_external_name(external_name: str) -> str:
    """Migration-local twin of import_service.normalize_external_name."""
    compatible = unicodedata.normalize("NFKC", external_name)
    collapsed = " ".join(compatible.split())
    return unicodedata.normalize("NFKC", collapsed.casefold())


def normalized_external_name_sha256(external_name: str) -> str:
    normalized_name = normalize_external_name(external_name)
    return hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.add_column(
        "exercise_import_aliases",
        sa.Column("normalized_external_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "exercise_import_aliases",
        sa.Column(
            "normalized_external_name_sha256",
            sa.String(length=64),
            nullable=True,
        ),
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
    normalized_rows = []
    normalized_by_digest: dict[str, str] = {}
    for row in rows:
        normalized_name = normalize_external_name(row["external_name"])
        if len(normalized_name) > NORMALIZED_EXTERNAL_NAME_MAX_CHARS:
            raise RuntimeError(
                "legacy exercise alias exceeds normalized storage contract"
            )
        normalized_name_sha256 = hashlib.sha256(
            normalized_name.encode("utf-8")
        ).hexdigest()
        previous_name = normalized_by_digest.setdefault(
            normalized_name_sha256, normalized_name
        )
        if previous_name != normalized_name:
            # Never let a cryptographic collision silently merge identities.
            raise RuntimeError("exercise alias identity digest collision")
        normalized_rows.append(
            {
                "row_id": row["id"],
                "normalized_name": normalized_name,
                "normalized_name_sha256": normalized_name_sha256,
            }
        )
    if normalized_rows:
        bind.execute(
            sa.text(
                """
                UPDATE exercise_import_aliases
                SET normalized_external_name = :normalized_name,
                    normalized_external_name_sha256 = :normalized_name_sha256
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
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "exercise_import_aliases",
        "normalized_external_name_sha256",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_exercise_import_alias_source",
        "exercise_import_aliases",
        "source IN ('strong', 'hevy', 'fitbod', 'table', 'notes')",
    )
    op.create_check_constraint(
        "ck_exercise_import_alias_normalized_length",
        "exercise_import_aliases",
        "char_length(normalized_external_name) <= 3600",
    )
    op.create_unique_constraint(
        "uq_exercise_import_alias_user_source_name",
        "exercise_import_aliases",
        ["app_user_id", "source", "normalized_external_name_sha256"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    # The pre-migration reader has no source dimension. Keep the table stable
    # while we prove every collapsed identity has one target and reconcile the
    # safe duplicates. Divergent choices must be handled explicitly by an
    # operator rather than becoming nondeterministic after downgrade.
    bind.execute(
        sa.text(
            "LOCK TABLE exercise_import_aliases IN SHARE ROW EXCLUSIVE MODE"
        )
    )
    rows = bind.execute(
        sa.text(
            """
            SELECT id, app_user_id, normalized_external_name, exercise_id
            FROM exercise_import_aliases
            ORDER BY id
            """
        )
    ).mappings().all()
    rows_by_legacy_identity: dict[tuple[int, str], list[dict]] = {}
    for row in rows:
        identity = (row["app_user_id"], row["normalized_external_name"])
        rows_by_legacy_identity.setdefault(identity, []).append(row)

    duplicate_ids: list[int] = []
    for identity, identity_rows in rows_by_legacy_identity.items():
        target_ids = {row["exercise_id"] for row in identity_rows}
        if len(target_ids) > 1:
            raise RuntimeError(
                "downgrade refused: divergent exercise alias targets for "
                f"user {identity[0]} and normalized name {identity[1]!r}"
            )
        duplicate_ids.extend(row["id"] for row in identity_rows[:-1])

    if duplicate_ids:
        bind.execute(
            sa.text(
                """
                DELETE FROM exercise_import_aliases
                WHERE id = :row_id
                """
            ),
            [{"row_id": row_id} for row_id in duplicate_ids],
        )

    op.drop_constraint(
        "uq_exercise_import_alias_user_source_name",
        "exercise_import_aliases",
        type_="unique",
    )
    op.drop_constraint(
        "ck_exercise_import_alias_normalized_length",
        "exercise_import_aliases",
        type_="check",
    )
    op.drop_constraint(
        "ck_exercise_import_alias_source",
        "exercise_import_aliases",
        type_="check",
    )
    op.drop_column("exercise_import_aliases", "normalized_external_name_sha256")
    op.drop_column("exercise_import_aliases", "normalized_external_name")
