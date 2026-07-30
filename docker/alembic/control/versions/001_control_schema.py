"""Establish the control schema migration ledger."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "001_control_schema"
down_revision = None
branch_labels = ("expand",)
depends_on = None


def upgrade() -> None:
    _execute_sql("001_control_schema.sql")


def downgrade() -> None:
    # Application rollback never performs destructive schema reversal.
    pass


def _execute_sql(file_name: str) -> None:
    migration = Path(__file__).resolve().parents[3] / "migrations" / "control" / file_name
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)
