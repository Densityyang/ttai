from __future__ import annotations

from typing import Any, cast

import pytest

from src.nl2sql.infra.governance.query_gateway import QueryGateway
from src.nl2sql.observability.control_audit import ControlAuditStore, ControlAuditUnavailable
from src.nl2sql.observability.trace import TraceEnvelope, TraceEvent


class _Transaction:
    """Minimal asyncpg-like transaction: commits on success, rolls back on error."""

    def __init__(self, connection: "_Connection") -> None:
        self._connection = connection

    async def __aenter__(self) -> None:
        self._connection.transactions += 1

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        del exc, tb
        if exc_type is None:
            self._connection.durable.extend(self._connection.pending)
        else:
            self._connection.rolled_back = True
        self._connection.pending.clear()
        return False


class _Connection:
    """Pool-like double: acquire() hands out the one connection per transaction."""

    def __init__(self, *, fail_on_outbox: bool = False) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.acquired = 0
        self.transactions = 0
        self.pending: list[str] = []
        self.durable: list[str] = []
        self.rolled_back = False
        self.fail_on_outbox = fail_on_outbox

    def acquire(self) -> "_Connection":
        self.acquired += 1
        return self

    async def __aenter__(self) -> "_Connection":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    async def execute(self, query: str, *args: object) -> object:
        self.calls.append((query, args))
        if "audit_outbox" in query and self.fail_on_outbox:
            raise RuntimeError("outbox insert failed")
        self.pending.append("audit_outbox" if "audit_outbox" in query else "audit_events")
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
    # Both rows are written on one acquired connection inside one transaction.
    assert connection.acquired == 1
    assert connection.transactions == 1
    assert connection.rolled_back is False
    assert connection.durable == ["audit_events", "audit_outbox"]


@pytest.mark.asyncio
async def test_control_audit_rolls_back_the_event_when_the_outbox_insert_fails() -> None:
    connection = _Connection(fail_on_outbox=True)
    store = ControlAuditStore(connection)

    with pytest.raises(ControlAuditUnavailable) as exc_info:
        await store.append(
            TraceEvent(trace_id="trace-1", stage="sql", name="execution_started")
        )

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert connection.acquired == 1
    assert connection.transactions == 1
    assert connection.rolled_back is True
    # The audit_events row inserted first must not survive the failed outbox row.
    assert connection.durable == []
    assert connection.pending == []


@pytest.mark.asyncio
async def test_control_audit_open_fails_closed_when_migrations_are_missing(monkeypatch) -> None:
    import asyncpg

    pool = _Pool(schema_ready=False)

    async def create_pool(*args: object, **kwargs: object) -> _Pool:
        del args, kwargs
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    with pytest.raises(ControlAuditUnavailable, match="not migrated"):
        await ControlAuditStore.open("postgresql://control_app:redacted@db/control")
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
        trace.record(
            stage,  # type: ignore[arg-type]
            "observed",
            prompt="private",
            prompt_hash="a" * 64,
            prompt_version="prompt-v1",
            rows=[{"token": "secret"}],
        )

    assert [event.stage for event in trace.events] == ["query", "retrieval", "candidate", "policy", "sql", "answer"]
    assert all(event.attributes["prompt"] == "[REDACTED]" for event in trace.events)
    assert all(event.attributes["rows"] == "[REDACTED]" for event in trace.events)
    assert all(event.attributes["prompt_hash"] == "a" * 64 for event in trace.events)
    assert all(event.attributes["prompt_version"] == "prompt-v1" for event in trace.events)
    assert TraceEvent.model_validate(trace.events[0]).trace_id == "trace-1"

def test_trace_envelope_scrubs_technical_secrets_value_level() -> None:
    trace = TraceEnvelope(trace_id="trace-1")

    event = trace.record(
        "answer",
        "observed",
        note="password = 'hunter2'",
        nested={"credential": "postgresql://svc:hunter2@db/app"},
    )

    assert event.attributes["note"] == "[REDACTED]"
    assert "hunter2" not in event.attributes["nested"]["credential"]
    assert event.attributes["nested"]["credential"].startswith("[REDACTED]")

def test_trace_key_redaction_is_exact_not_substring() -> None:
    trace = TraceEnvelope(trace_id="trace-1")

    event = trace.record(
        "answer",
        "observed",
        # Legitimate telemetry that merely CONTAINS a sensitive word.
        authorization_count=3,
        authorization_revision="rev-1",
        tokenize_count=12,
        token_cost=120,
        input_tokens=10,
        secret_version="rotate-2026-07",
        # Exact credential field names stay redacted in any normalised spelling.
        Authorization="Bearer abcdefgh",
        api_key="k",
        **{"api-key": "k", "access_token": "t", "client_secret": "s", "db_password": "p"},
    )

    assert event.attributes["authorization_count"] == 3
    assert event.attributes["authorization_revision"] == "rev-1"
    assert event.attributes["tokenize_count"] == 12
    assert event.attributes["token_cost"] == 120
    assert event.attributes["input_tokens"] == 10
    assert event.attributes["secret_version"] == "rotate-2026-07"
    assert event.attributes["Authorization"] == "[REDACTED]"
    assert event.attributes["api_key"] == "[REDACTED]"
    assert event.attributes["api-key"] == "[REDACTED]"
    assert event.attributes["access_token"] == "[REDACTED]"
    assert event.attributes["client_secret"] == "[REDACTED]"
    assert event.attributes["db_password"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_control_audit_scrubs_technical_secrets_before_persisting() -> None:
    connection = _Connection()
    store = ControlAuditStore(connection)
    event = TraceEvent(
        trace_id="trace-1",
        stage="answer",
        name="observed",
        attributes={"note": "password = 'hunter2'", "answer": "revenue 100"},
    )

    await store.append(event)

    audit_payload = cast(str, connection.calls[0][1][5])
    outbox_payload = cast(str, connection.calls[1][1][4])
    assert "hunter2" not in audit_payload
    assert "hunter2" not in outbox_payload
    assert "revenue 100" in audit_payload
