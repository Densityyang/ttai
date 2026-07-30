"""Alembic environment for the LangGraph checkpoint PostgreSQL database."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from src.core.secrets import SecretProvider


def _database_url() -> str:
    value = SecretProvider().get("CHECKPOINT_MIGRATOR_DATABASE_URL")
    if not value:
        raise RuntimeError("checkpoint migrator URL secret is empty")
    return value


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="alembic_version",
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        connect_args={
            "connect_timeout": 5,
            "application_name": "ttai-checkpoint-migrator",
            "options": "-c statement_timeout=300000 -c lock_timeout=10000",
        },
    )
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                version_table="alembic_version",
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
