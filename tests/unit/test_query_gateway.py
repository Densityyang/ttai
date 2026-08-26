"""Contract tests for the process-local QueryGateway boundary."""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text

from src.nl2sql.infra.governance.query_gateway import (
    PolicyEngine,
    QueryErrorCode,
    QueryGateway,
    QueryPolicyError,
    _classify_database_error,
    _mask_rows,
    _parse_plan,
    _plan_metrics,
)
from src.nl2sql.infra.store.database import DatabaseManager
from src.nl2sql.tools.async_sql_tools import create_async_sql_tools


class _FakeResult:
    def __init__(
        self,
        *,
        plan: Any = None,
        rows: list[tuple[Any, ...]] | None = None,
        keys: list[str] | None = None,
    ) -> None:
        self._plan = plan
        self._rows = rows or []
        self._keys = keys or ["id", "api_key"]

    def fetchone(self) -> tuple[Any] | None:
        return (self._plan,) if self._plan is not None else None

    def keys(self) -> list[str]:
        return self._keys

    async def partitions(self, size: int) -> Any:
        for index in range(0, len(self._rows), size):
            yield self._rows[index : index + size]


class _FakeSession:
    def __init__(
        self,
        *,
        plan: Any = None,
        rows: list[tuple[Any, ...]] | None = None,
        keys: list[str] | None = None,
        explain_error: Exception | None = None,
        stream_error: Exception | None = None,
        explain_delay_seconds: float = 0.0,
    ) -> None:
        self.plan = plan
        self.rows = rows
        self.keys = keys
        self.explain_error = explain_error
        self.stream_error = stream_error
        self.explain_delay_seconds = explain_delay_seconds
        self.commands: list[str] = []
        self.parameters: list[dict[str, Any]] = []
        self.rolled_back = False

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(
        self,
        statement: object,
        params: dict[str, Any] | None = None,
    ) -> _FakeResult:
        rendered = str(statement)
        self.commands.append(rendered)
        self.parameters.append(dict(params or {}))
        if rendered.startswith("EXPLAIN"):
            if self.explain_delay_seconds:
                await asyncio.sleep(self.explain_delay_seconds)
            if self.explain_error is not None:
                raise self.explain_error
            return _FakeResult(plan=self.plan)
        return _FakeResult()

    async def stream(
        self,
        statement: object,
        params: dict[str, Any] | None = None,
    ) -> _FakeResult:
        self.commands.append(str(statement))
        self.parameters.append(dict(params or {}))
        if self.stream_error is not None:
            raise self.stream_error
        return _FakeResult(rows=self.rows, keys=self.keys)

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeSessionFactory:
    def __init__(self, *sessions: _FakeSession) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> _FakeSession:
        return self.sessions.pop(0)


class _FakeAuditSink:
    def __init__(self, *, fail_start: bool = False, fail_result: bool = False) -> None:
        self.fail_start = fail_start
        self.fail_result = fail_result
        self.starts: list[tuple[str, str]] = []
        self.results: list[dict[str, Any]] = []

    async def record_sql_start(self, trace_id: str, sql: str) -> None:
        if self.fail_start:
            raise RuntimeError("audit connection detail")
        self.starts.append((trace_id, sql))

    async def record_sql_result(self, trace_id: str, sql: str, **values: Any) -> None:
        if self.fail_result:
            raise RuntimeError("audit connection detail")
        self.results.append({"trace_id": trace_id, "sql": sql, **values})


def test_policy_rewrites_limit_and_qualifies_business_tables() -> None:
    policy = PolicyEngine(max_rows=25, allowed_schema="ai_views")

    prepared = policy.prepare("SELECT * FROM orders")
    existing = policy.prepare("SELECT * FROM orders LIMIT 5")
    clamped = policy.prepare("SELECT * FROM orders LIMIT 100")

    assert prepared.sql == "SELECT * FROM ai_views.orders LIMIT 25"
    assert prepared.tables == ("ai_views.orders",)
    assert prepared.data_scope == ("ai_views",)
    assert len(prepared.fingerprint) == 64
    assert existing.sql.endswith("LIMIT 5")
    assert clamped.sql.endswith("LIMIT 25")


def test_policy_preserves_named_parameters_for_sqlalchemy() -> None:
    policy = PolicyEngine(max_rows=10, allowed_schema="ai_views")

    prepared = policy.prepare(
        "SELECT '%(metric_code)s' AS literal, metric_code "
        "FROM v_metric_result WHERE metric_code = :metric_code"
    )

    assert "'%(metric_code)s' AS literal" in prepared.sql
    assert "metric_code = :metric_code" in prepared.sql
    assert prepared.parameter_names == ("metric_code",)
    assert list(text(prepared.bind_sql)._bindparams) == ["metric_code"]
    assert policy.validate_params(prepared, {"metric_code": "revenue"}) == {
        "metric_code": "revenue"
    }


def test_policy_does_not_turn_literal_or_comment_colons_into_binds() -> None:
    policy = PolicyEngine(max_rows=10)

    prepared = policy.prepare(
        "SELECT :real AS bound, ':fake' AS literal /* :comment */"
    )

    assert prepared.parameter_names == ("real",)
    assert list(text(prepared.bind_sql)._bindparams) == ["real"]
    assert ":comment" not in prepared.sql
    assert "':fake'" in prepared.sql


@pytest.mark.parametrize(
    ("params", "message_fragment"),
    [
        ({}, "missing: id"),
        ({"id": 1, "extra": 2}, "unexpected: extra"),
        ({"bad-name": 1, "id": 2}, "simple identifiers"),
    ],
)
def test_policy_requires_exact_parameter_set(
    params: dict[str, Any],
    message_fragment: str,
) -> None:
    policy = PolicyEngine()
    prepared = policy.prepare("SELECT * FROM orders WHERE id = :id")

    with pytest.raises(QueryPolicyError) as exc_info:
        policy.validate_params(prepared, params)

    assert exc_info.value.code == QueryErrorCode.PARAMETER_MISMATCH
    assert message_fragment in str(exc_info.value)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT 1; SELECT 2", QueryErrorCode.POLICY_DENIED),
        ("INSERT INTO orders VALUES (1)", QueryErrorCode.POLICY_DENIED),
        ("WITH changed AS (DELETE FROM orders RETURNING *) SELECT * FROM changed", QueryErrorCode.POLICY_DENIED),
        ("SELECT * FROM orders FOR UPDATE", QueryErrorCode.POLICY_DENIED),
        ("SELECT * FROM (SELECT * FROM orders FOR SHARE) AS locked", QueryErrorCode.POLICY_DENIED),
        ("SELECT pg_sleep(1)", QueryErrorCode.POLICY_DENIED),
        ("SELECT nextval('order_seq')", QueryErrorCode.POLICY_DENIED),
        ("SELECT set_config('search_path', 'public', false)", QueryErrorCode.POLICY_DENIED),
        ("SELECT query_to_xml('SELECT * FROM public.orders', true, false, '')", QueryErrorCode.POLICY_DENIED),
        ("SELECT public.untrusted_function()", QueryErrorCode.SCHEMA_DENIED),
        ("SELECT * FROM orders LIMIT :limit", QueryErrorCode.POLICY_DENIED),
        ("SELECT * FROM orders WHERE id = $1", QueryErrorCode.PARAMETER_MISMATCH),
        ("SELECT * FROM orders, customers", QueryErrorCode.POLICY_DENIED),
        ("SELECT * FROM orders CROSS JOIN customers", QueryErrorCode.POLICY_DENIED),
        ("SELECT * FROM public.orders", QueryErrorCode.SCHEMA_DENIED),
        ("SELECT * FROM pg_catalog.pg_class", QueryErrorCode.SCHEMA_DENIED),
        ("SELECT * FROM database.ai_views.orders", QueryErrorCode.SCHEMA_DENIED),
    ],
)
def test_policy_is_fail_closed(sql: str, code: QueryErrorCode) -> None:
    with pytest.raises(QueryPolicyError) as exc_info:
        PolicyEngine(allowed_schema="ai_views").prepare(sql)

    assert exc_info.value.code == code


def test_policy_resolves_cte_scope_without_system_catalog_escape() -> None:
    policy = PolicyEngine(max_rows=20, allowed_schema="ai_views")

    prepared = policy.prepare(
        "WITH selected AS (SELECT * FROM orders WHERE note = 'DELETE') "
        "SELECT * FROM selected"
    )
    system_name = policy.prepare("SELECT * FROM pg_class")

    assert prepared.sql == (
        "WITH selected AS (SELECT * FROM ai_views.orders WHERE note = 'DELETE') "
        "SELECT * FROM selected LIMIT 20"
    )
    assert prepared.tables == ("ai_views.orders",)
    assert system_name.sql == "SELECT * FROM ai_views.pg_class LIMIT 20"


def test_policy_supports_bounded_fetch_and_correlated_lateral_queries() -> None:
    policy = PolicyEngine(max_rows=20, allowed_schema="ai_views")

    fetched = policy.prepare("SELECT * FROM orders FETCH FIRST 10 ROWS ONLY")
    lateral = policy.prepare(
        "SELECT orders.id, item.value FROM orders "
        "CROSS JOIN LATERAL jsonb_array_elements_text(orders.items) AS item(value)"
    )

    assert fetched.sql == "SELECT * FROM ai_views.orders LIMIT 10"
    assert "CROSS JOIN LATERAL" in lateral.sql
    with pytest.raises(QueryPolicyError):
        policy.prepare("SELECT * FROM orders FETCH FIRST 10 ROWS WITH TIES")


def test_policy_rejects_excessive_query_nesting_and_ctes() -> None:
    deep = "SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM t) c) b) a"
    many_ctes = (
        "WITH a AS (SELECT 1), b AS (SELECT 1), c AS (SELECT 1) SELECT * FROM a"
    )

    with pytest.raises(QueryPolicyError):
        PolicyEngine(max_query_depth=3).prepare(deep)
    with pytest.raises(QueryPolicyError):
        PolicyEngine(max_ctes=2).prepare(many_ctes)


def test_result_masking_and_plan_parsing() -> None:
    rows, masked_columns = _mask_rows([{"id": 1, "api_key": "private", "name": "ok"}])
    plan = _parse_plan(
        [
            {
                "Plan": {
                    "Total Cost": 12.5,
                    "Plan Rows": 10,
                    "Plans": [{"Plan Rows": 500}],
                }
            }
        ]
    )

    assert rows == [{"id": 1, "api_key": "[REDACTED]", "name": "ok"}]
    assert masked_columns == {"api_key"}
    assert _plan_metrics(plan) == (12.5, 500)


@pytest.mark.parametrize(
    ("sqlstate", "code", "retryable"),
    [
        ("08006", QueryErrorCode.CONNECTION_ERROR, True),
        ("40001", QueryErrorCode.TRANSIENT_DATABASE_ERROR, True),
        ("42501", QueryErrorCode.PERMISSION_DENIED, False),
        ("22007", QueryErrorCode.PARAMETER_INVALID, False),
        ("42P18", QueryErrorCode.PARAMETER_INVALID, False),
        ("42P01", QueryErrorCode.RELATION_NOT_FOUND, False),
    ],
)
def test_database_error_taxonomy_is_safe_and_retry_aware(
    sqlstate: str,
    code: QueryErrorCode,
    retryable: bool,
) -> None:
    error = RuntimeError("driver detail containing password=secret")
    error.sqlstate = sqlstate  # type: ignore[attr-defined]

    actual_code, message, actual_retryable = _classify_database_error(
        error,
        stage="execute",
    )

    assert actual_code == code
    assert actual_retryable is retryable
    assert "password" not in message
    assert "secret" not in message


@pytest.mark.parametrize(
    "raw_plan",
    [None, [], [{}], [{"Plan": {"Plan Rows": 1}}], [{"Plan": {"Total Cost": 1}}]],
)
def test_plan_parser_and_metrics_fail_closed(raw_plan: Any) -> None:
    with pytest.raises((TypeError, ValueError, KeyError)):
        plan = _parse_plan(raw_plan)
        _plan_metrics(plan)


@pytest.mark.asyncio
async def test_gateway_binds_params_to_plan_and_streams_bounded_masked_rows() -> None:
    session = _FakeSession(
        plan=[
            {
                "Plan": {
                    "Total Cost": 12,
                    "Plan Rows": 2,
                    "Plans": [{"Plan Rows": 20}],
                }
            }
        ],
        rows=[(1, "private")],
        keys=["id", "api_key"],
    )
    gateway = QueryGateway(
        cast(Any, _FakeSessionFactory(session)),
        schema="ai_views",
        max_rows=10,
        readonly_role="business_reader",
    )

    receipt = await gateway.execute(
        "SELECT id, api_key FROM orders WHERE id = :id",
        {"id": 1},
    )

    assert receipt.accepted
    assert receipt.sql == (
        "SELECT id, api_key FROM ai_views.orders WHERE id = :id LIMIT 10"
    )
    assert receipt.rows == [{"id": 1, "api_key": "[REDACTED]"}]
    assert receipt.estimated_cost == 12
    assert receipt.estimated_rows == 20
    assert receipt.masked_columns == ("api_key",)
    assert session.rolled_back
    assert [
        params
        for command, params in zip(session.commands, session.parameters, strict=True)
        if command.startswith("EXPLAIN") or command.startswith("SELECT")
    ] == [{"id": 1}, {"id": 1}]
    assert list(text(receipt.sql)._bindparams) == ["id"]
    assert "SET TRANSACTION READ ONLY" in session.commands
    assert any("statement_timeout" in command for command in session.commands)
    assert receipt.policy_decision.outcome == "allow"
    assert receipt.execution_receipt.sql_fingerprint == receipt.sql_fingerprint
    assert receipt.execution_receipt.masking_applied


@pytest.mark.asyncio
async def test_gateway_rejects_parameter_mismatch_before_opening_session() -> None:
    factory = _FakeSessionFactory()
    gateway = QueryGateway(cast(Any, factory))

    receipt = await gateway.execute("SELECT * FROM orders WHERE id = :id")

    assert not receipt.accepted
    assert receipt.error is not None
    assert receipt.error.code == QueryErrorCode.PARAMETER_MISMATCH
    assert factory.sessions == []


@pytest.mark.asyncio
async def test_gateway_rejects_expensive_or_wide_plan_before_execution() -> None:
    cost_session = _FakeSession(plan=[{"Plan": {"Total Cost": 999, "Plan Rows": 1}}])
    row_session = _FakeSession(
        plan=[
            {
                "Plan": {
                    "Total Cost": 1,
                    "Plan Rows": 10,
                    "Plans": [{"Plan Rows": 50_000}],
                }
            }
        ]
    )

    cost_receipt = await QueryGateway(
        cast(Any, _FakeSessionFactory(cost_session)), max_plan_cost=100
    ).execute("SELECT * FROM orders")
    row_receipt = await QueryGateway(
        cast(Any, _FakeSessionFactory(row_session)), max_plan_rows=100
    ).execute("SELECT * FROM orders")

    assert cost_receipt.error is not None
    assert cost_receipt.error.code == QueryErrorCode.COST_EXCEEDED
    assert cost_receipt.estimated_cost == 999
    assert row_receipt.error is not None
    assert row_receipt.error.code == QueryErrorCode.ROWS_EXCEEDED
    assert row_receipt.estimated_rows == 50_000


@pytest.mark.asyncio
async def test_gateway_fails_closed_on_invalid_explain_without_leaking_driver_detail() -> None:
    secret_detail = "postgresql://user:password@private-db/query literal='secret'"
    plan_session = _FakeSession(explain_error=RuntimeError(secret_detail))
    gateway = QueryGateway(cast(Any, _FakeSessionFactory(plan_session)))

    receipt = await gateway.execute("SELECT * FROM missing_relation")

    assert not receipt.accepted
    assert receipt.error is not None
    assert receipt.error.code == QueryErrorCode.PLAN_FAILED
    assert receipt.error.message == "query planning failed"
    assert secret_detail not in receipt.error.message
    assert receipt.policy_outcome == "allow"


@pytest.mark.asyncio
async def test_gateway_enforces_result_bytes_while_streaming() -> None:
    session = _FakeSession(
        plan=[{"Plan": {"Total Cost": 1, "Plan Rows": 1}}],
        rows=[("x" * 1_000,)],
        keys=["payload"],
    )
    gateway = QueryGateway(
        cast(Any, _FakeSessionFactory(session)),
        max_result_bytes=100,
    )

    receipt = await gateway.execute("SELECT payload FROM orders")

    assert not receipt.accepted
    assert receipt.error is not None
    assert receipt.error.code == QueryErrorCode.RESULT_TOO_LARGE
    assert receipt.estimated_cost == 1
    assert receipt.rows == []
    assert session.rolled_back


@pytest.mark.asyncio
async def test_gateway_classifies_timeout_as_retryable() -> None:
    session = _FakeSession(
        plan=[{"Plan": {"Total Cost": 1, "Plan Rows": 1}}],
        stream_error=TimeoutError("driver detail"),
    )
    gateway = QueryGateway(cast(Any, _FakeSessionFactory(session)))

    receipt = await gateway.execute("SELECT * FROM orders")

    assert receipt.error is not None
    assert receipt.error.code == QueryErrorCode.TIMEOUT
    assert receipt.error.retryable
    assert "driver detail" not in receipt.error.message


@pytest.mark.asyncio
async def test_gateway_has_an_independent_fail_closed_explain_timeout() -> None:
    session = _FakeSession(
        plan=[{"Plan": {"Total Cost": 1, "Plan Rows": 1}}],
        explain_delay_seconds=0.05,
    )
    receipt = await QueryGateway(
        cast(Any, _FakeSessionFactory(session)),
        timeout_seconds=2,
        plan_timeout_ms=10,
    ).execute("SELECT * FROM orders")

    assert not receipt.accepted
    assert receipt.error is not None
    assert receipt.error.code == QueryErrorCode.TIMEOUT
    assert receipt.error.retryable
    assert session.rolled_back
    assert not any(command.startswith("SELECT") for command in session.commands)


@pytest.mark.asyncio
async def test_gateway_audit_is_fail_closed_and_records_structured_result() -> None:
    blocked_factory = _FakeSessionFactory()
    blocked = await QueryGateway(
        cast(Any, blocked_factory),
        audit_sink=_FakeAuditSink(fail_start=True),
    ).execute("SELECT 1", trace_id="trace-blocked")

    session = _FakeSession(
        plan=[{"Plan": {"Total Cost": 1, "Plan Rows": 1}}],
        rows=[(1,)],
        keys=["value"],
    )
    audit = _FakeAuditSink()
    accepted = await QueryGateway(
        cast(Any, _FakeSessionFactory(session)),
        audit_sink=audit,
    ).execute("SELECT 1 AS value", trace_id="trace-ok")

    assert blocked.error is not None
    assert blocked.error.code == QueryErrorCode.AUDIT_UNAVAILABLE
    assert blocked_factory.sessions == []
    assert accepted.accepted
    assert audit.starts == [("trace-ok", "SELECT 1 AS value")]
    assert audit.results[0]["accepted"] is True
    assert audit.results[0]["row_count"] == 1
    assert audit.results[0]["error_code"] is None


@pytest.mark.asyncio
async def test_gateway_discards_result_when_required_audit_result_fails() -> None:
    session = _FakeSession(
        plan=[{"Plan": {"Total Cost": 1, "Plan Rows": 1}}],
        rows=[(1,)],
        keys=["value"],
    )
    receipt = await QueryGateway(
        cast(Any, _FakeSessionFactory(session)),
        audit_sink=_FakeAuditSink(fail_result=True),
    ).execute("SELECT 1 AS value", trace_id="trace-failed-result")

    assert not receipt.accepted
    assert receipt.rows == []
    assert receipt.error is not None
    assert receipt.error.code == QueryErrorCode.AUDIT_UNAVAILABLE


def test_application_query_paths_delegate_to_gateway() -> None:
    database_source = inspect.getsource(DatabaseManager.execute_query)
    tool_source = inspect.getsource(create_async_sql_tools)
    gateway_source = inspect.getsource(QueryGateway._begin_read_only)

    assert "self.query(" in database_source
    assert "session.execute" not in database_source
    assert "def session(" not in inspect.getsource(DatabaseManager)
    assert "db_manager.query(query)" in tool_source
    assert "SET TRANSACTION READ ONLY" in gateway_source
    assert "statement_timeout" in gateway_source
    assert "lock_timeout" in gateway_source
    assert "idle_in_transaction_session_timeout" in gateway_source


def test_all_database_execution_calls_are_explicitly_allowlisted() -> None:
    root = Path(__file__).resolve().parents[2]
    # Include non-database ``execute``/``stream`` calls as reviewed exceptions.
    # Any new call with one of these high-risk method names changes the exact
    # inventory and forces an explicit review of whether it bypasses the gateway.
    allowed_calls = {
        "src/core/observer.py": {"runnable.stream"},
        "src/nl2sql/infra/governance/query_gateway.py": {
            "session.execute",
            "session.stream",
        },
        "src/nl2sql/infra/store/database.py": {
            "conn.execute",
            "self._query_gateway.execute",
        },
        "src/nl2sql/infra/store/ai_views.py": {"conn.execute"},
        "src/nl2sql/observability/control_audit.py": {
            "pool.fetchval",
            "self._connection.execute",
        },
            "src/nl2sql/semantic/registry.py": {"connection.execute"},
            "src/nl2sql/semantic/schema_snapshot.py": {"connection.execute"},
            "src/nl2sql/agents/codeact_engine/graph.py": {"sandbox.execute"},
        "src/nl2sql/agents/dynamic_calc/graph.py": {
            "executor.execute",
            "trusted_template_registry.execute",
        },
        "src/nl2sql/orchestration/engine.py": {"plan_executor.execute"},
        "src/nl2sql/orchestration/execution.py": {
            "self._metric_runner.execute",
            "self._registry.execute",
            "self._trusted_calculation_runner.execute",
        },
    }
    database_methods = {
        "execute",
        "executemany",
        "fetch",
        "fetchrow",
        "fetchval",
        "scalar",
        "scalars",
        "stream",
    }
    discovered: dict[str, set[str]] = {}
    factory_allowlist = {
        "src/core/database.py": {"create_async_engine"},
        "src/nl2sql/observability/control_audit.py": {"asyncpg.create_pool"},
    }
    discovered_factories: dict[str, set[str]] = {}

    for path in (root / "src").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        direct_factory_aliases: dict[str, str] = {}
        module_aliases: dict[str, str] = {}
        for import_node in ast.walk(tree):
            if isinstance(import_node, ast.Import):
                for alias in import_node.names:
                    if alias.name in {"asyncpg", "psycopg"}:
                        module_aliases[alias.asname or alias.name] = alias.name
            elif isinstance(import_node, ast.ImportFrom):
                module = import_node.module or ""
                for alias in import_node.names:
                    local_name = alias.asname or alias.name
                    if module.startswith("sqlalchemy") and alias.name in {
                        "create_async_engine",
                        "create_engine",
                    }:
                        direct_factory_aliases[local_name] = alias.name
                    elif module in {"asyncpg", "psycopg"} and alias.name in {
                        "connect",
                        "create_pool",
                    }:
                        direct_factory_aliases[local_name] = f"{module}.{alias.name}"
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            factory_name: str | None = None
            if isinstance(node.func, ast.Name):
                factory_name = direct_factory_aliases.get(node.func.id)
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_aliases
            ):
                factory_name = f"{module_aliases[node.func.value.id]}.{node.func.attr}"
            if factory_name is not None:
                discovered_factories.setdefault(relative, set()).add(factory_name)
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in database_methods:
                continue
            receiver = ast.unparse(node.func.value)
            call_name = f"{receiver}.{node.func.attr}"
            discovered.setdefault(relative, set()).add(call_name)

    assert discovered == allowed_calls
    assert discovered_factories == factory_allowlist
    legacy_guard = (
        root / "src/nl2sql/infra/governance/sql_guard.py"
    ).read_text(encoding="utf-8")
    assert "session.execute" not in legacy_guard


def test_business_agents_have_no_direct_database_session_access() -> None:
    root = Path(__file__).resolve().parents[2]
    agent_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (root / "src/nl2sql/agents").rglob("*.py")
    )

    assert "session.execute" not in agent_source
    assert "connection.execute" not in agent_source
    assert "create_async_sql_tools(db_manager)" in agent_source
