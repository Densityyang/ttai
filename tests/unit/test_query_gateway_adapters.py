"""Regression tests for production adapters that feed the QueryGateway."""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pytest

from src.nl2sql.infra.governance.query_gateway import (
    QueryError,
    QueryErrorCode,
    QueryReceipt,
)
from src.nl2sql.infra.store.database import DatabaseManager
from src.nl2sql.semantic.metric_layer import MetricSemanticLayer
from src.nl2sql.tools.async_sql_tools import create_async_sql_tools


class _MetricDatabase:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def execute_query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        return []


@pytest.mark.asyncio
async def test_metric_layer_converts_iso_dates_to_typed_bind_values() -> None:
    database = _MetricDatabase()
    layer = MetricSemanticLayer(cast(DatabaseManager, database))

    await layer.query(
        metric_code="revenue",
        time_grain="day",
        start_date="2026-07-01",
        end_date="2026-07-02",
    )

    _, params = database.calls[0]
    assert params is not None
    assert params["start_date"] == date(2026, 7, 1)
    assert params["end_date"] == date(2026, 7, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_date", "end_date", "message"),
    [
        ("not-a-date", "2026-07-02", "ISO-8601"),
        ("2026-07-03", "2026-07-02", "later than"),
    ],
)
async def test_metric_layer_rejects_invalid_date_ranges_before_database(
    start_date: str,
    end_date: str,
    message: str,
) -> None:
    database = _MetricDatabase()
    layer = MetricSemanticLayer(cast(DatabaseManager, database))

    with pytest.raises(ValueError, match=message):
        await layer.query(
            metric_code="revenue",
            time_grain="day",
            start_date=start_date,
            end_date=end_date,
        )

    assert database.calls == []


class _ToolDatabase:
    schema = "ai_views"

    def __init__(
        self,
        *,
        receipt: QueryReceipt | None = None,
        error: Exception | None = None,
    ) -> None:
        self.receipt = receipt
        self.error = error

    def get_table_names(self) -> list[str]:
        return ["orders"]

    def get_schema_description(self, _: list[str]) -> str:
        return "orders(id integer)"

    async def query(self, _: str) -> QueryReceipt:
        if self.error is not None:
            raise self.error
        assert self.receipt is not None
        return self.receipt


def _query_tool(database: _ToolDatabase) -> Any:
    return next(
        tool
        for tool in create_async_sql_tools(cast(DatabaseManager, database))
        if tool.name == "sql_db_query"
    )


@pytest.mark.asyncio
async def test_agent_query_tool_returns_safe_taxonomy_without_driver_details() -> None:
    receipt = QueryReceipt(
        accepted=False,
        sql="",
        error=QueryError(
            code=QueryErrorCode.PLAN_FAILED,
            message="query planning failed",
        ),
    )
    rejected = await _query_tool(_ToolDatabase(receipt=receipt)).ainvoke(
        {"query": "SELECT * FROM missing"}
    )
    secret = "postgresql://user:password@private-db/database"
    failed = await _query_tool(_ToolDatabase(error=RuntimeError(secret))).ainvoke(
        {"query": "SELECT 1"}
    )

    assert rejected == (
        "查询验证失败 [code=plan_failed retryable=false]: query planning failed"
    )
    assert "code=database_error" in failed
    assert secret not in failed
