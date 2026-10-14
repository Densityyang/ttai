"""Persist Custom Definitions so published versions cannot dangle."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "006_definition_store"
down_revision = "005_product_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "control"
        / "006_definition_store.sql"
    )
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    pass
