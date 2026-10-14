"""Persist atomic owner-scoped HITL action claims."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "009_hitl_action_claims"
down_revision = "008_definition_run_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "control"
        / "009_hitl_action_claims.sql"
    )
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    pass
