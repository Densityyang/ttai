"""One-shot LangGraph checkpoint schema migration entry point."""

from __future__ import annotations

from langgraph.checkpoint.postgres import PostgresSaver

from src.core.database import DatabasePurpose, build_psycopg_runtime_dsn
from src.core.secrets import SecretProvider
from src.core.settings import Settings


def migrate_checkpoint_schema() -> None:
    """Apply LangGraph-owned migrations with the dedicated migrator credential."""

    database_url = SecretProvider().get("CHECKPOINT_MIGRATOR_DATABASE_URL")
    if not database_url:
        raise RuntimeError("CHECKPOINT_MIGRATOR_DATABASE_URL_FILE is required")

    runtime_url = build_psycopg_runtime_dsn(
        database_url,
        purpose=DatabasePurpose.CHECKPOINT_MIGRATOR,
        application_name="ttai-checkpoint-migrator",
        settings=Settings(
            auth_enabled=False,
            service_mode="infra-dev",
            memory_backend="memory",
        ),
    )
    with PostgresSaver.from_conn_string(runtime_url) as saver:
        saver.setup()


def main() -> None:
    migrate_checkpoint_schema()


if __name__ == "__main__":
    main()
