"""Database runtime boundaries shared by business, control, and checkpoint stores."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class DatabasePurpose(StrEnum):
    """The least-privilege purpose attached to a runtime database connection."""

    BUSINESS_READ_ONLY = "business_read_only"
    CONTROL_APP = "control_app"
    CHECKPOINT_APP = "checkpoint_app"
    CHECKPOINT_MIGRATOR = "checkpoint_migrator"


_PRIVILEGED_ROLE_TOKENS = {
    "admin",
    "backup",
    "dba",
    "migrator",
    "owner",
    "root",
    "superuser",
}
_APPLICATION_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def validate_application_database_url(database_url: str, purpose: DatabasePurpose) -> None:
    """Reject owner, migration, and administrative credentials from API settings."""

    try:
        url = make_url(database_url)
    except Exception as exc:
        raise ValueError(f"invalid {purpose.value} database URL") from exc

    if not url.drivername.startswith("postgresql"):
        raise ValueError(f"{purpose.value} must use PostgreSQL")

    username = (url.username or "").strip().lower()
    if not username:
        raise ValueError(f"{purpose.value} database URL must include a role")

    role_tokens = {token for token in re.split(r"[^a-z0-9]+", username) if token}
    if username == "postgres" or role_tokens.intersection(_PRIVILEGED_ROLE_TOKENS):
        raise ValueError(f"{purpose.value} database URL uses a privileged role")


def build_async_engine_kwargs(
    database_url: str,
    *,
    purpose: DatabasePurpose,
    application_name: str,
    settings: Any,
) -> dict[str, Any]:
    """Build bounded SQLAlchemy pool and PostgreSQL session settings."""

    url = make_url(database_url)
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if not url.drivername.startswith("postgresql"):
        return kwargs

    kwargs.update(
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        pool_recycle=settings.database_pool_recycle_seconds,
    )
    server_settings = _postgres_server_settings(
        purpose=purpose,
        application_name=application_name,
        statement_timeout_ms=settings.database_statement_timeout_ms,
        lock_timeout_ms=settings.database_lock_timeout_ms,
        idle_transaction_timeout_ms=settings.database_idle_transaction_timeout_ms,
    )

    if "asyncpg" in url.drivername:
        kwargs["connect_args"] = {
            "timeout": settings.database_connect_timeout_seconds,
            "command_timeout": settings.database_statement_timeout_ms / 1000,
            "server_settings": server_settings,
        }
    elif "psycopg" in url.drivername:
        options = " ".join(
            f"-c {key}={value}"
            for key, value in server_settings.items()
            if key != "application_name"
        )
        kwargs["connect_args"] = {
            "connect_timeout": settings.database_connect_timeout_seconds,
            "application_name": server_settings["application_name"],
            "options": options,
        }
    return kwargs


def create_runtime_async_engine(
    database_url: str,
    *,
    purpose: DatabasePurpose,
    application_name: str,
    settings: Any,
) -> AsyncEngine:
    """Create an async engine with the shared connection and timeout contract."""

    if make_url(database_url).drivername.startswith("postgresql"):
        validate_application_database_url(database_url, purpose)
    kwargs = build_async_engine_kwargs(
        database_url,
        purpose=purpose,
        application_name=application_name,
        settings=settings,
    )
    return create_async_engine(database_url, **kwargs)


def build_psycopg_runtime_dsn(
    database_url: str,
    *,
    purpose: DatabasePurpose,
    application_name: str,
    settings: Any,
) -> str:
    """Attach bounded session options for libraries that open psycopg directly."""

    if purpose is not DatabasePurpose.CHECKPOINT_MIGRATOR:
        validate_application_database_url(database_url, purpose)
    parsed = urlsplit(database_url)
    scheme = parsed.scheme.split("+", maxsplit=1)[0]
    if scheme not in {"postgres", "postgresql"}:
        raise ValueError(f"{purpose.value} must use PostgreSQL")

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    server_settings = _postgres_server_settings(
        purpose=purpose,
        application_name=application_name,
        statement_timeout_ms=settings.database_statement_timeout_ms,
        lock_timeout_ms=settings.database_lock_timeout_ms,
        idle_transaction_timeout_ms=settings.database_idle_transaction_timeout_ms,
    )
    query.setdefault("connect_timeout", str(settings.database_connect_timeout_seconds))
    query.setdefault("application_name", server_settings.pop("application_name"))
    query.setdefault(
        "options",
        " ".join(f"-c {key}={value}" for key, value in server_settings.items()),
    )
    return urlunsplit(
        (
            scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query, quote_via=quote),
            parsed.fragment,
        )
    )


def _postgres_server_settings(
    *,
    purpose: DatabasePurpose,
    application_name: str,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
    idle_transaction_timeout_ms: int,
) -> dict[str, str]:
    settings = {
        "application_name": _normalize_application_name(application_name),
        "statement_timeout": str(statement_timeout_ms),
        "lock_timeout": str(lock_timeout_ms),
        "idle_in_transaction_session_timeout": str(idle_transaction_timeout_ms),
    }
    if purpose is DatabasePurpose.BUSINESS_READ_ONLY:
        settings["default_transaction_read_only"] = "on"
    return settings


def _normalize_application_name(value: str) -> str:
    normalized = _APPLICATION_NAME_PATTERN.sub("-", value.strip()).strip("-")
    return (normalized or "ttai")[:63]
