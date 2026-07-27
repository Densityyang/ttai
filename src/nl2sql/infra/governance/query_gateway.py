"""The only application-level SQL execution boundary.

The gateway keeps policy evaluation, PostgreSQL planning, transaction guards and
result shaping in one in-process component.  Callers receive a structured
receipt instead of accessing a SQLAlchemy session directly.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import sqlglot
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlglot import exp


class QueryErrorCode(StrEnum):
    INVALID_SQL = "invalid_sql"
    POLICY_DENIED = "policy_denied"
    PLAN_FAILED = "plan_failed"
    COST_EXCEEDED = "cost_exceeded"
    ROWS_EXCEEDED = "rows_exceeded"
    TIMEOUT = "timeout"
    DATABASE_ERROR = "database_error"
    RESULT_TOO_LARGE = "result_too_large"


@dataclass(frozen=True)
class QueryError:
    code: QueryErrorCode
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class QueryReceipt:
    accepted: bool
    sql: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    estimated_cost: float | None = None
    estimated_rows: int | None = None
    elapsed_ms: float = 0.0
    masked_columns: tuple[str, ...] = ()
    error: QueryError | None = None


class QueryGatewayError(RuntimeError):
    """Raised by legacy adapters when a receipt is rejected."""

    def __init__(self, error: QueryError) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.error = error


class QueryPolicyError(ValueError):
    def __init__(self, code: QueryErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


_SENSITIVE_COLUMN = re.compile(r"(?:password|secret|token|api[_-]?key|authorization|credential)", re.I)
_SAFE_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORBIDDEN_EXPRESSIONS = (
    exp.Alter,
    exp.Command,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Grant,
    exp.Insert,
    exp.Into,
    exp.Merge,
    exp.Revoke,
    exp.TruncateTable,
    exp.Update,
)
_FORBIDDEN_FUNCTIONS = {"dblink", "lo_export", "pg_read_file", "pg_sleep"}


class PolicyEngine:
    """Fail-closed sqlglot policy for a single PostgreSQL read query."""

    def __init__(self, *, max_rows: int = 200) -> None:
        self.max_rows = max_rows

    def prepare(self, sql: str) -> str:
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except Exception as exc:
            raise QueryPolicyError(QueryErrorCode.INVALID_SQL, f"SQL parse failed: {exc}") from exc

        if len(statements) != 1:
            raise QueryPolicyError(QueryErrorCode.POLICY_DENIED, "exactly one SQL statement is required")

        query = statements[0]
        if not isinstance(query, exp.Query):
            raise QueryPolicyError(QueryErrorCode.POLICY_DENIED, "only read-only SELECT queries are allowed")
        if query.args.get("locks"):
            raise QueryPolicyError(QueryErrorCode.POLICY_DENIED, "locking SELECT queries are not allowed")
        if any(query.find(kind) is not None for kind in _FORBIDDEN_EXPRESSIONS):
            raise QueryPolicyError(QueryErrorCode.POLICY_DENIED, "write or DDL expressions are not allowed")

        for function in query.find_all(exp.Anonymous):
            if function.name.lower() in _FORBIDDEN_FUNCTIONS:
                raise QueryPolicyError(QueryErrorCode.POLICY_DENIED, f"function {function.name} is not allowed")

        limit = query.args.get("limit")
        if limit is not None:
            literal = limit.expression
            if not isinstance(literal, exp.Literal) or not literal.is_int:
                raise QueryPolicyError(QueryErrorCode.POLICY_DENIED, "LIMIT must be an integer literal")
            safe_limit = min(int(literal.this), self.max_rows)
        else:
            safe_limit = self.max_rows

        return query.copy().limit(safe_limit).sql(dialect="postgres")


class QueryGateway:
    """Execute policy-approved queries with database-enforced read-only limits."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        schema: str | None = None,
        timeout_seconds: int = 30,
        lock_timeout_ms: int = 1_000,
        idle_timeout_ms: int = 10_000,
        max_rows: int = 200,
        max_plan_cost: float = 500_000.0,
        max_plan_rows: int = 100_000,
        max_result_bytes: int = 1_000_000,
    ) -> None:
        if schema and not _SAFE_SCHEMA.fullmatch(schema):
            raise ValueError(f"invalid schema identifier: {schema}")
        self._session_factory = session_factory
        self._schema = schema
        self._timeout_seconds = timeout_seconds
        self._lock_timeout_ms = lock_timeout_ms
        self._idle_timeout_ms = idle_timeout_ms
        self._max_plan_cost = max_plan_cost
        self._max_plan_rows = max_plan_rows
        self._max_result_bytes = max_result_bytes
        self._policy = PolicyEngine(max_rows=max_rows)

    def prepare(self, sql: str) -> str:
        return self._policy.prepare(sql)

    async def preflight(self, sql: str) -> QueryReceipt:
        started = time.perf_counter()
        try:
            safe_sql = self.prepare(sql)
            cost, rows = await self._plan(safe_sql)
            return QueryReceipt(
                accepted=True,
                sql=safe_sql,
                estimated_cost=cost,
                estimated_rows=rows,
                elapsed_ms=_elapsed_ms(started),
            )
        except QueryPolicyError as exc:
            return _rejected(sql, exc.code, str(exc), started)
        except TimeoutError:
            return _rejected(sql, QueryErrorCode.TIMEOUT, "query planning timed out", started, retryable=True)
        except Exception as exc:
            return _rejected(sql, QueryErrorCode.PLAN_FAILED, "query planning failed", started, detail=exc)

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> QueryReceipt:
        from src.nl2sql.infra.governance.semaphore import get_concurrency_governor

        governor = get_concurrency_governor()
        async with governor.acquire("sql"):
            return await self._execute_locked(sql, params)

    async def _execute_locked(self, sql: str, params: dict[str, Any] | None = None) -> QueryReceipt:
        started = time.perf_counter()
        try:
            safe_sql = self.prepare(sql)
            cost, rows_estimate = await self._plan(safe_sql)
            rows, masked_columns = await self._run(safe_sql, params or {})
            serialized = json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
            if len(serialized) > self._max_result_bytes:
                return _rejected(
                    safe_sql,
                    QueryErrorCode.RESULT_TOO_LARGE,
                    f"query result exceeds {self._max_result_bytes} bytes",
                    started,
                    estimated_cost=cost,
                    estimated_rows=rows_estimate,
                )
            return QueryReceipt(
                accepted=True,
                sql=safe_sql,
                rows=rows,
                row_count=len(rows),
                estimated_cost=cost,
                estimated_rows=rows_estimate,
                elapsed_ms=_elapsed_ms(started),
                masked_columns=tuple(sorted(masked_columns)),
            )
        except QueryPolicyError as exc:
            return _rejected(sql, exc.code, str(exc), started)
        except TimeoutError:
            return _rejected(sql, QueryErrorCode.TIMEOUT, "query execution timed out", started, retryable=True)
        except Exception as exc:
            return _rejected(sql, QueryErrorCode.DATABASE_ERROR, "query execution failed", started, detail=exc)

    async def _plan(self, safe_sql: str) -> tuple[float, int]:
        async with self._session_factory() as session:
            try:
                await self._begin_read_only(session)
                result = await asyncio.wait_for(
                    session.execute(text(f"EXPLAIN (FORMAT JSON) {safe_sql}")),
                    timeout=self._timeout_seconds,
                )
                row = result.fetchone()
            finally:
                await session.rollback()

        plan = _parse_plan(row[0] if row is not None else None)
        cost = float(plan.get("Total Cost", 0.0))
        rows = int(plan.get("Plan Rows", 0))
        if cost > self._max_plan_cost:
            raise QueryPolicyError(QueryErrorCode.COST_EXCEEDED, f"estimated cost {cost:.0f} exceeds policy")
        if rows > self._max_plan_rows:
            raise QueryPolicyError(QueryErrorCode.ROWS_EXCEEDED, f"estimated rows {rows} exceeds policy")
        return cost, rows

    async def _run(self, safe_sql: str, params: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
        async with self._session_factory() as session:
            try:
                await self._begin_read_only(session)
                result = await asyncio.wait_for(
                    session.execute(text(safe_sql), params),
                    timeout=self._timeout_seconds,
                )
                keys = list(result.keys())
                rows = [dict(zip(keys, row)) for row in result.fetchall()]
            finally:
                await session.rollback()
        return _mask_rows(rows)

    async def _begin_read_only(self, session: AsyncSession) -> None:
        await session.execute(text("BEGIN READ ONLY"))
        await session.execute(text(f"SET LOCAL statement_timeout = '{self._timeout_seconds}s'"))
        await session.execute(text(f"SET LOCAL lock_timeout = '{self._lock_timeout_ms}ms'"))
        await session.execute(text(f"SET LOCAL idle_in_transaction_session_timeout = '{self._idle_timeout_ms}ms'"))
        if self._schema:
            await session.execute(text(f"SET LOCAL search_path TO {self._schema}"))


def _parse_plan(raw_plan: Any) -> dict[str, Any]:
    if isinstance(raw_plan, str):
        raw_plan = json.loads(raw_plan)
    if isinstance(raw_plan, list) and raw_plan:
        plan = raw_plan[0].get("Plan")
    elif isinstance(raw_plan, dict):
        plan = raw_plan.get("Plan")
    else:
        plan = None
    if not isinstance(plan, dict):
        raise ValueError("EXPLAIN returned no plan")
    return plan


def _mask_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    masked_columns: set[str] = set()
    safe_rows: list[dict[str, Any]] = []
    for row in rows:
        safe_row: dict[str, Any] = {}
        for key, value in row.items():
            if _SENSITIVE_COLUMN.search(key):
                safe_row[key] = "[REDACTED]"
                masked_columns.add(key)
            else:
                safe_row[key] = value
        safe_rows.append(safe_row)
    return safe_rows, masked_columns


def _rejected(
    sql: str,
    code: QueryErrorCode,
    message: str,
    started: float,
    *,
    retryable: bool = False,
    detail: Exception | None = None,
    estimated_cost: float | None = None,
    estimated_rows: int | None = None,
) -> QueryReceipt:
    if detail is not None:
        message = f"{message}: {detail}"
    return QueryReceipt(
        accepted=False,
        sql=sql,
        estimated_cost=estimated_cost,
        estimated_rows=estimated_rows,
        elapsed_ms=_elapsed_ms(started),
        error=QueryError(code=code, message=message, retryable=retryable),
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
