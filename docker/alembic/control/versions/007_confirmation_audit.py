"""Persist confirmation audit and exploration confirmations so they survive a restart."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "007_confirmation_audit"
down_revision = "006_definition_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "control"
        / "007_confirmation_audit.sql"
    )
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    pass
