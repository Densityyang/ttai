"""Persist immutable, owner-scoped Custom Definition run records."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "008_definition_run_records"
down_revision = "007_confirmation_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "control"
        / "008_definition_run_records.sql"
    )
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    pass
