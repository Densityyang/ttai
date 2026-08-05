"""Add the typed schema-v3 semantic registry and safe release allocation."""

from __future__ import annotations

from pathlib import Path

import sqlparse
from alembic import op

revision = "004_semantic_registry_v3"
down_revision = "003_audit_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "control"
        / "004_semantic_registry_v3.sql"
    )
    for statement in sqlparse.split(migration.read_text(encoding="utf-8")):
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    pass
