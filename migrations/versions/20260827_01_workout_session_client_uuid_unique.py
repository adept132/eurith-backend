"""Enforce per-user workout client UUID idempotency.

Revision ID: 20261018_p113_identity
Revises: 20261018_p113_display
Create Date: 2026-08-27
"""

from typing import Sequence, Union
import re

from alembic import op
import sqlalchemy as sa


revision: str = "20261018_p113_identity"
down_revision: Union[str, Sequence[str], None] = "20261018_p113_display"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

IDENTITY_NAME = "uq_workout_sessions_user_client_uuid"
OWNER_COMMENT = "eurith:migration:20261018_p113_identity"


def _existing_identity_protection(bind) -> bool:
    index = next((item for item in sa.inspect(bind).get_indexes("workout_sessions")
                  if item["name"] == IDENTITY_NAME), None)
    if index is None:
        return False
    predicate = (index.get("dialect_options") or {}).get("postgresql_where")
    normalized = re.sub(r'[\s()"]', '', str(predicate)).lower() if predicate is not None else None
    valid = bind.scalar(sa.text("""
        SELECT i.indisunique AND i.indisvalid AND i.indisready AND i.indislive
        FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
        WHERE i.indrelid = 'workout_sessions'::regclass AND c.relname = :name
    """), {"name": IDENTITY_NAME})
    if not (valid and index.get("unique")
            and index.get("column_names") == ["app_user_id", "client_uuid"]
            and normalized in (None, "client_uuidisnotnull")):
        raise RuntimeError("existing workout identity index does not enforce the required identity")
    return True


def upgrade() -> None:
    bind = op.get_bind()
    # Serialize the duplicate audit and constraint creation with writers. We
    # refuse to discard ambiguous workout history automatically; an operator
    # can reconcile any legacy duplicates without losing user data.
    bind.execute(
        sa.text("LOCK TABLE workout_sessions IN SHARE ROW EXCLUSIVE MODE")
    )
    duplicate_identity_count = bind.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM (
                SELECT app_user_id, client_uuid
                FROM workout_sessions
                WHERE client_uuid IS NOT NULL
                GROUP BY app_user_id, client_uuid
                HAVING count(*) > 1
            ) AS duplicate_identities
            """
        )
    )
    if duplicate_identity_count:
        raise RuntimeError(
            "cannot enforce workout client UUID uniqueness: "
            f"{duplicate_identity_count} duplicate identities require reconciliation"
        )

    # The released database may already have the equivalent partial unique
    # index. Preserve its identity and ownership rather than duplicating it.
    if _existing_identity_protection(bind):
        return
    op.create_unique_constraint(
        IDENTITY_NAME,
        "workout_sessions",
        ["app_user_id", "client_uuid"],
    )
    op.execute(sa.text(f"COMMENT ON CONSTRAINT {IDENTITY_NAME} ON workout_sessions IS '{OWNER_COMMENT}'"))


def downgrade() -> None:
    comment = op.get_bind().scalar(sa.text("""
        SELECT obj_description(oid, 'pg_constraint') FROM pg_constraint
        WHERE conrelid = 'workout_sessions'::regclass AND conname = :name AND contype = 'u'
    """), {"name": IDENTITY_NAME})
    # A pre-existing index/constraint belongs to the earlier schema.
    if comment != OWNER_COMMENT:
        return
    op.drop_constraint(
        IDENTITY_NAME,
        "workout_sessions",
        type_="unique",
    )
