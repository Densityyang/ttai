"""Add immutable semantic releases and retrieval indexes."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "002_semantic_registry"
down_revision = "001_control_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "control"
        / "002_semantic_registry.sql"
    )
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)

def downgrade() -> None:
    pass
