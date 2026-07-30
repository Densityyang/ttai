"""Establish the checkpoint schema migration ledger."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "001_checkpoint_schema"
down_revision = None
branch_labels = ("expand",)
depends_on = None


def upgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "checkpoint"
        / "001_checkpoint_schema.sql"
    )
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)

def downgrade() -> None:
    pass
