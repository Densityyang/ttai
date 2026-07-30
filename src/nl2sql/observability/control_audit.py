"""Control-plane audit/outbox writer.

The writer is deliberately independent of Langfuse and OTel.  Runtime actions
are first recorded durably in control PostgreSQL; external observability tools
consume the generated outbox records asynchronously.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import uuid4

from src.core.database import DatabasePurpose, validate_application_database_url
from src.core.settings import get_settings
from src.nl2sql.observability.trace import TraceEvent, fingerprint


class AuditConnection(Protocol):
    async def execute(self, query: str, *args: object) -> object: ...


class ControlAuditUnavailable(RuntimeError):
    """The control plane could not durably accept a required audit event."""


class ControlAuditStore:
    def __init__(
        self,
        connection: AuditConnection,
        *,
        close_callback: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._connection = connection
        self._close_callback = close_callback

    @classmethod
    async def open(cls, database_url: str) -> "ControlAuditStore":
        import asyncpg

        validate_application_database_url(database_url, DatabasePurpose.CONTROL_APP)
        settings = get_settings()
        pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=settings.database_pool_size + settings.database_max_overflow,
            timeout=settings.database_connect_timeout_seconds,
            command_timeout=settings.database_statement_timeout_ms / 1000,
            max_inactive_connection_lifetime=settings.database_pool_recycle_seconds,
            server_settings={
                "application_name": "ttai-control-audit",
                "statement_timeout": str(settings.database_statement_timeout_ms),
                "lock_timeout": str(settings.database_lock_timeout_ms),
                "idle_in_transaction_session_timeout": str(
                    settings.database_idle_transaction_timeout_ms
                ),
            },
        )
        try:
            schema_ready = await pool.fetchval(
                """
                SELECT to_regclass('public.audit_events') IS NOT NULL
                   AND to_regclass('public.audit_outbox') IS NOT NULL
                """
            )
        except Exception:
            await pool.close()
            raise
        if not schema_ready:
            await pool.close()
            raise ControlAuditUnavailable("control audit schema is not migrated")
        return cls(pool, close_callback=pool.close)

    async def close(self) -> None:
        if callable(self._close_callback):
            await self._close_callback()

    async def append(self, event: TraceEvent) -> None:
        event_id = str(uuid4())
        payload = event.model_dump(mode="json")
        try:
            await self._connection.execute(
                """
                INSERT INTO audit_events (event_id, trace_id, stage, event_name, occurred_at, attributes)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                event_id,
                event.trace_id,
                event.stage,
                event.name,
                event.at,
                json.dumps(event.attributes, ensure_ascii=False, default=str),
            )
            await self._connection.execute(
                """
                INSERT INTO audit_outbox (outbox_id, trace_id, event_id, topic, payload)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                str(uuid4()),
                event.trace_id,
                event_id,
                "nl2sql.trace",
                json.dumps(payload, ensure_ascii=False, default=str),
            )
        except Exception as exc:
            raise ControlAuditUnavailable("control audit/outbox write failed") from exc

    async def record_sql_start(self, trace_id: str, sql: str) -> None:
        await self.append(
            TraceEvent(
                trace_id=trace_id or "unscoped",
                stage="sql",
                name="execution_started",
                attributes={"sql_fingerprint": fingerprint(sql)},
            )
        )

    async def record_sql_result(
        self,
        trace_id: str,
        sql: str,
        *,
        accepted: bool,
        row_count: int,
        elapsed_ms: float,
        error_code: str | None,
    ) -> None:
        await self.append(
            TraceEvent(
                trace_id=trace_id or "unscoped",
                stage="sql",
                name="execution_finished",
                attributes={
                    "sql_fingerprint": fingerprint(sql),
                    "accepted": accepted,
                    "row_count": row_count,
                    "elapsed_ms": round(elapsed_ms, 3),
                    "error_code": error_code,
                },
            )
        )
