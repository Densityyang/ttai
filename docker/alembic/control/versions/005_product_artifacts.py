"""Add product persistence for artifacts, the publication catalogue and the library."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "005_product_artifacts"
down_revision = "004_semantic_registry_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "control"
        / "005_product_artifacts.sql"
    )
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    pass
