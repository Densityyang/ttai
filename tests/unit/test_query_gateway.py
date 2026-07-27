"""Contract tests for the process-local QueryGateway boundary."""

import inspect
from pathlib import Path
from typing import Any, cast

import pytest

from src.nl2sql.infra.governance.query_gateway import (
    PolicyEngine,
    QueryErrorCode,
    QueryGateway,
    QueryPolicyError,
    _mask_rows,
    _parse_plan,
)
from src.nl2sql.infra.store.database import DatabaseManager
from src.nl2sql.tools.async_sql_tools import create_async_sql_tools


class _FakeResult:
    def __init__(self, *, plan: Any = None, rows: list[tuple[Any, ...]] | None = None) -> None:
        self._plan = plan
        self._rows = rows or []

    def fetchone(self) -> tuple[Any] | None:
        return (self._plan,) if self._plan is not None else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def keys(self) -> list[str]:
        return ["id", "api_key"]


class _FakeSession:
    def __init__(self, *, plan: Any = None, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.plan = plan
        self.rows = rows
        self.commands: list[str] = []
        self.rolled_back = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, statement: object, _: object = None) -> _FakeResult:
        rendered = str(statement)
        self.commands.append(rendered)
        if rendered.startswith("EXPLAIN"):
            return _FakeResult(plan=self.plan)
        if rendered.startswith("SELECT"):
            return _FakeResult(rows=self.rows)
        return _FakeResult()

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeSessionFactory:
    def __init__(self, *sessions: _FakeSession) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> _FakeSession:
        return self.sessions.pop(0)


def test_policy_accepts_one_read_query_and_rewrites_limit() -> None:
    policy = PolicyEngine(max_rows=25)

    assert policy.prepare("SELECT * FROM orders") == "SELECT * FROM orders LIMIT 25"
    assert policy.prepare("SELECT * FROM orders LIMIT 5") == "SELECT * FROM orders LIMIT 5"
    assert policy.prepare("SELECT * FROM orders LIMIT 100") == "SELECT * FROM orders LIMIT 25"


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT 1; SELECT 2", QueryErrorCode.POLICY_DENIED),
        ("INSERT INTO orders VALUES (1)", QueryErrorCode.POLICY_DENIED),
        ("SELECT * FROM orders FOR UPDATE", QueryErrorCode.POLICY_DENIED),
        ("SELECT pg_sleep(1)", QueryErrorCode.POLICY_DENIED),
        ("SELECT * FROM orders LIMIT :limit", QueryErrorCode.POLICY_DENIED),
    ],
)
def test_policy_is_fail_closed(sql: str, code: QueryErrorCode) -> None:
    with pytest.raises(QueryPolicyError) as exc_info:
        PolicyEngine().prepare(sql)

    assert exc_info.value.code == code


def test_result_masking_and_plan_parsing() -> None:
    rows, masked_columns = _mask_rows([{"id": 1, "api_key": "private", "name": "ok"}])

    assert rows == [{"id": 1, "api_key": "[REDACTED]", "name": "ok"}]
    assert masked_columns == {"api_key"}
    assert _parse_plan([{"Plan": {"Total Cost": 1, "Plan Rows": 2}}]) == {
        "Total Cost": 1,
        "Plan Rows": 2,
    }


@pytest.mark.asyncio
async def test_gateway_returns_structured_read_only_receipt() -> None:
    plan_session = _FakeSession(plan=[{"Plan": {"Total Cost": 12, "Plan Rows": 2}}])
    query_session = _FakeSession(rows=[(1, "private")])
    gateway = QueryGateway(
        cast(Any, _FakeSessionFactory(plan_session, query_session)),
        max_rows=10,
    )

    receipt = await gateway.execute("SELECT id, api_key FROM orders")

    assert receipt.accepted
    assert receipt.sql == "SELECT id, api_key FROM orders LIMIT 10"
    assert receipt.rows == [{"id": 1, "api_key": "[REDACTED]"}]
    assert receipt.estimated_cost == 12
    assert receipt.estimated_rows == 2
    assert receipt.masked_columns == ("api_key",)
    assert plan_session.rolled_back and query_session.rolled_back
    assert "BEGIN READ ONLY" in plan_session.commands
    assert any("statement_timeout" in command for command in query_session.commands)


@pytest.mark.asyncio
async def test_gateway_rejects_expensive_plan_before_execution() -> None:
    plan_session = _FakeSession(plan=[{"Plan": {"Total Cost": 999, "Plan Rows": 1}}])
    gateway = QueryGateway(cast(Any, _FakeSessionFactory(plan_session)), max_plan_cost=100)

    receipt = await gateway.execute("SELECT * FROM orders")

    assert not receipt.accepted
    assert receipt.error is not None
    assert receipt.error.code == QueryErrorCode.COST_EXCEEDED


def test_application_query_paths_delegate_to_gateway() -> None:
    database_source = inspect.getsource(DatabaseManager.execute_query)
    tool_source = inspect.getsource(create_async_sql_tools)
    gateway_source = inspect.getsource(QueryGateway._begin_read_only)

    assert "self.query(" in database_source
    assert "session.execute" not in database_source
    assert "def session(" not in inspect.getsource(DatabaseManager)
    assert "db_manager.query(query)" in tool_source
    assert "BEGIN READ ONLY" in gateway_source
    assert "statement_timeout" in gateway_source
    assert "lock_timeout" in gateway_source
    assert "idle_in_transaction_session_timeout" in gateway_source


def test_nl2sql_gen_data_and_dynamic_calc_have_no_direct_session_execution() -> None:
    root = Path(__file__).resolve().parents[2]
    selected_sources = [
        root / "src/nl2sql/agents/gen_data/tools.py",
        root / "src/nl2sql/agents/dynamic_calc/graph.py",
        root / "src/nl2sql/tools/async_sql_tools.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in selected_sources)

    assert "session.execute" not in source
    assert "create_async_sql_tools(db_manager)" in source
    assert "db_manager.query(query)" in source
