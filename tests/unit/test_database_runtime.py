from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from src.core.database import (
    DatabasePurpose,
    build_async_engine_kwargs,
    build_psycopg_runtime_dsn,
    validate_application_database_url,
)
from src.core.settings import Settings


def _pool_settings() -> SimpleNamespace:
    return SimpleNamespace(
        database_pool_size=3,
        database_max_overflow=2,
        database_pool_timeout_seconds=3.0,
        database_pool_recycle_seconds=900,
        database_connect_timeout_seconds=3,
        database_statement_timeout_ms=30_000,
        database_lock_timeout_ms=3_000,
        database_idle_transaction_timeout_ms=30_000,
    )


def test_business_engine_is_bounded_and_read_only() -> None:
    kwargs = build_async_engine_kwargs(
        "postgresql+asyncpg://business_reader:secret@db/business",
        purpose=DatabasePurpose.BUSINESS_READ_ONLY,
        application_name="ttai business query",
        settings=_pool_settings(),
    )

    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_size"] == 3
    assert kwargs["max_overflow"] == 2
    assert kwargs["pool_timeout"] == 3.0
    assert kwargs["pool_recycle"] == 900
    assert kwargs["connect_args"]["timeout"] == 3
    assert kwargs["connect_args"]["command_timeout"] == 30
    server_settings = kwargs["connect_args"]["server_settings"]
    assert server_settings["default_transaction_read_only"] == "on"
    assert server_settings["statement_timeout"] == "30000"
    assert server_settings["lock_timeout"] == "3000"
    assert server_settings["application_name"] == "ttai-business-query"


def test_checkpoint_psycopg_dsn_has_bounded_session_options() -> None:
    dsn = build_psycopg_runtime_dsn(
        "postgresql://checkpoint_app:secret@db/checkpoint?sslmode=require",
        purpose=DatabasePurpose.CHECKPOINT_APP,
        application_name="ttai-checkpoint",
        settings=_pool_settings(),
    )
    query = parse_qs(urlsplit(dsn).query)

    assert query["sslmode"] == ["require"]
    assert query["connect_timeout"] == ["3"]
    assert query["application_name"] == ["ttai-checkpoint"]
    assert "statement_timeout=30000" in query["options"][0]
    assert "default_transaction_read_only" not in query["options"][0]
    assert "+" not in urlsplit(dsn).query
    assert "%20" in urlsplit(dsn).query


def test_checkpoint_migrator_dsn_is_explicitly_separate_from_runtime_roles() -> None:
    dsn = build_psycopg_runtime_dsn(
        "postgresql://checkpoint_migrator:secret@db/checkpoint",
        purpose=DatabasePurpose.CHECKPOINT_MIGRATOR,
        application_name="ttai-checkpoint-migrator",
        settings=_pool_settings(),
    )

    assert urlsplit(dsn).username == "checkpoint_migrator"


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://postgres:secret@db/business",
        "postgresql+asyncpg://control_owner:secret@db/control",
        "postgresql+asyncpg://checkpoint_migrator:secret@db/checkpoint",
        "postgresql+asyncpg://control_backup:secret@db/control",
        "postgresql+asyncpg://platform_admin:secret@db/control",
    ],
)
def test_runtime_database_urls_reject_privileged_roles(database_url: str) -> None:
    with pytest.raises(ValueError, match="privileged role"):
        validate_application_database_url(database_url, DatabasePurpose.CONTROL_APP)


def test_product_settings_accept_only_separated_application_roles() -> None:
    settings = Settings(
        _env_file=None,
        auth_enabled=False,
        service_mode="product",
        memory_backend="postgresql",
        database_url="postgresql+asyncpg://business_reader:secret@db/business",
        control_database_url="postgresql+asyncpg://control_app:secret@db/control",
        checkpoint_database_url="postgresql://checkpoint_app:secret@db/checkpoint",
    )

    assert settings.database_pool_size == 3
    assert settings.database_max_overflow == 2


def test_product_settings_fail_closed_for_owner_credentials() -> None:
    with pytest.raises(ValidationError, match="privileged role"):
        Settings(
            _env_file=None,
            auth_enabled=False,
            service_mode="product",
            memory_backend="postgresql",
            database_url="postgresql+asyncpg://postgres:secret@db/business",
            control_database_url="postgresql+asyncpg://control_app:secret@db/control",
            checkpoint_database_url="postgresql://checkpoint_app:secret@db/checkpoint",
        )
