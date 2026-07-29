from __future__ import annotations

from typing import Any, cast

import pytest

from src.nl2sql.infra.governance.query_gateway import QueryGateway
from src.nl2sql.observability.control_audit import ControlAuditStore, ControlAuditUnavailable
from src.nl2sql.observability.trace import TraceEnvelope, TraceEvent


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> object:
        self.calls.append((query, args))
        return "INSERT 0 1"


class _UnavailableAudit:
    async def record_sql_start(self, trace_id: str, sql: str) -> None:
        del trace_id, sql
        raise RuntimeError("control database down")

    async def record_sql_result(self, **kwargs: Any) -> None:
        del kwargs


class _Pool:
    def __init__(self, schema_ready: bool) -> None:
        self.schema_ready = schema_ready
        self.closed = False

    async def fetchval(self, query: str) -> bool:
        assert "audit_events" in query
        assert "audit_outbox" in query
        return self.schema_ready

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_control_audit_writes_event_and_outbox_without_raw_sql() -> None:
    connection = _Connection()
    store = ControlAuditStore(connection)

    await store.record_sql_start("trace-1", "SELECT * FROM customers WHERE password = 'secret'")

    assert len(connection.calls) == 2
    event_args = connection.calls[0][1]
    assert event_args[2] == "sql"
    assert "SELECT" not in str(event_args)
    assert connection.calls[1][1][3] == "nl2sql.trace"


@pytest.mark.asyncio
async def test_control_audit_open_fails_closed_when_migrations_are_missing(monkeypatch) -> None:
    import asyncpg

    pool = _Pool(schema_ready=False)

    async def create_pool(*args: object, **kwargs: object) -> _Pool:
        del args, kwargs
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    with pytest.raises(ControlAuditUnavailable, match="not migrated"):
        await ControlAuditStore.open("postgresql://redacted")
    assert pool.closed is True


@pytest.mark.asyncio
async def test_query_gateway_fails_closed_when_required_control_audit_is_unavailable() -> None:
    gateway = QueryGateway(cast(Any, None), audit_sink=_UnavailableAudit())

    receipt = await gateway.execute("SELECT 1", trace_id="trace-1")

    assert receipt.accepted is False
    assert receipt.error is not None
    assert "audit" in receipt.error.message


def test_trace_schema_redacts_sensitive_attributes_and_covers_all_stages() -> None:
    trace = TraceEnvelope(trace_id="trace-1")
    for stage in ("query", "retrieval", "candidate", "policy", "sql", "answer"):
        trace.record(stage, "observed", prompt="private", rows=[{"token": "secret"}])  # type: ignore[arg-type]

    assert [event.stage for event in trace.events] == ["query", "retrieval", "candidate", "policy", "sql", "answer"]
    assert all(event.attributes["prompt"] == "[REDACTED]" for event in trace.events)
    assert all(event.attributes["rows"] == "[REDACTED]" for event in trace.events)
    assert TraceEvent.model_validate(trace.events[0]).trace_id == "trace-1"
