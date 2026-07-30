"""Add the durable audit outbox and benchmark run registry."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "003_audit_outbox"
down_revision = "002_semantic_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "control"
        / "003_audit_outbox.sql"
    )
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)

def downgrade() -> None:
    pass
