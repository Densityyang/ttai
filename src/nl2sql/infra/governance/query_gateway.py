"""The single application boundary for executing business SQL.

Every caller is forced through the same SQLGlot policy, PostgreSQL EXPLAIN
gate, read-only transaction and bounded result reader.  The boundary is kept
in-process so policy evaluation adds no network hop to latency-sensitive agent
paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import uuid4

import sqlglot
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

from src.nl2sql.contracts import ExecutionReceipt, PolicyDecision

logger = logging.getLogger(__name__)

POLICY_VERSION = "query-gateway-v2"


class QueryErrorCode(StrEnum):
    INVALID_SQL = "invalid_sql"
    PARAMETER_MISMATCH = "parameter_mismatch"
    PARAMETER_INVALID = "parameter_invalid"
    POLICY_DENIED = "policy_denied"
    SCHEMA_DENIED = "schema_denied"
    PLAN_FAILED = "plan_failed"
    COST_EXCEEDED = "cost_exceeded"
    ROWS_EXCEEDED = "rows_exceeded"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    TRANSIENT_DATABASE_ERROR = "transient_database_error"
    PERMISSION_DENIED = "permission_denied"
    RELATION_NOT_FOUND = "relation_not_found"
    DATABASE_ERROR = "database_error"
    RESULT_TOO_LARGE = "result_too_large"
    AUDIT_UNAVAILABLE = "audit_unavailable"


@dataclass(frozen=True)
class QueryError:
    code: QueryErrorCode
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class PreparedQuery:
    """Immutable result of policy evaluation, ready for SQLAlchemy binding."""

    sql: str
    bind_sql: str
    fingerprint: str
    parameter_names: tuple[str, ...]
    tables: tuple[str, ...]
    max_rows: int
    data_scope: tuple[str, ...]
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True)
class QueryReceipt:
    accepted: bool
    sql: str
    sql_fingerprint: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    estimated_cost: float | None = None
    estimated_rows: int | None = None
    elapsed_ms: float = 0.0
    masked_columns: tuple[str, ...] = ()
    error: QueryError | None = None
    datasource: str = "business_postgres"
    readonly_role: str = "business_reader"
    policy_version: str = POLICY_VERSION
    policy_outcome: Literal["allow", "deny"] = "deny"
    max_rows: int | None = None
    timeout_ms: int | None = None
    data_scope: tuple[str, ...] = ()

    @property
    def policy_decision(self) -> PolicyDecision:
        """Expose the shared agent-boundary policy contract without duplication."""

        return PolicyDecision(
            outcome=self.policy_outcome,
            max_rows=self.max_rows,
            timeout_ms=self.timeout_ms,
            data_scope=self.data_scope,
            reason=self.error.message if self.policy_outcome == "deny" and self.error else None,
        )

    @property
    def execution_receipt(self) -> ExecutionReceipt:
        """Project the gateway result into the versioned execution contract."""

        return ExecutionReceipt(
            datasource=self.datasource,
            readonly_role=self.readonly_role,
            elapsed_ms=max(0, round(self.elapsed_ms)),
            row_count=self.row_count,
            plan_cost=self.estimated_cost,
            estimated_rows=self.estimated_rows,
            masking_applied=bool(self.masked_columns),
            masked_columns=self.masked_columns,
            error_taxonomy=self.error.code if self.error else None,
            sql_fingerprint=self.sql_fingerprint,
            policy_version=self.policy_version,
            policy_outcome=self.policy_outcome,
        )


class QueryGatewayError(RuntimeError):
    """Raised by adapters that require exception-style gateway failures."""

    def __init__(self, error: QueryError) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.error = error


class QueryPolicyError(ValueError):
    def __init__(
        self,
        code: QueryErrorCode,
        message: str,
        *,
        estimated_cost: float | None = None,
        estimated_rows: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.estimated_cost = estimated_cost
        self.estimated_rows = estimated_rows


class _QueryDatabaseFailure(RuntimeError):
    """Carry a caught driver failure and its safe execution stage internally."""

    def __init__(
        self,
        stage: Literal["plan", "execute"],
        error: Exception,
        *,
        estimated_cost: float | None = None,
        estimated_rows: int | None = None,
    ) -> None:
        super().__init__("query database operation failed")
        self.stage: Literal["plan", "execute"] = stage
        self.error = error
        self.estimated_cost = estimated_cost
        self.estimated_rows = estimated_rows


class QueryAuditSink(Protocol):
    async def record_sql_start(self, trace_id: str, sql: str) -> None: ...

    async def record_sql_result(
        self,
        trace_id: str,
        sql: str,
        *,
        accepted: bool,
        row_count: int,
        elapsed_ms: float,
        error_code: str | None,
    ) -> None: ...


_SENSITIVE_COLUMN = re.compile(
    r"(?:password|secret|token|api[_-]?key|authorization|credential)", re.I
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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
_FORBIDDEN_FUNCTIONS = {
    "dblink",
    "lo_export",
    "lo_import",
    "lo_unlink",
    "nextval",
    "pg_cancel_backend",
    "pg_create_restore_point",
    "pg_logical_emit_message",
    "pg_ls_dir",
    "pg_notify",
    "pg_promote",
    "pg_read_binary_file",
    "pg_read_file",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_sleep",
    "pg_stat_file",
    "pg_switch_wal",
    "pg_terminate_backend",
    "query_to_xml",
    "query_to_xml_and_xmlschema",
    "query_to_xmlschema",
    "set_config",
    "setval",
}
_FORBIDDEN_FUNCTION_PREFIXES = (
    "dblink_",
    "lo_",
    "pg_advisory_",
    "pg_backup_",
    "pg_ls_",
    "pg_replication_origin_",
    "pg_wal_replay_",
)
_PARAMETER_MARKER_PREFIX = "__ttai_sqlalchemy_bind_"
_SQLALCHEMY_FALSE_BIND = re.compile(r":(?=\w)", re.UNICODE)
_SQLGLOT_STRING_EXPRESSIONS = (
    exp.ByteString,
    exp.Heredoc,
    exp.RawString,
    exp.UnicodeString,
)


class PolicyEngine:
    """Fail-closed SQLGlot policy for one bounded PostgreSQL read query."""

    def __init__(
        self,
        *,
        max_rows: int = 200,
        allowed_schema: str | None = None,
        max_ctes: int = 5,
        max_query_depth: int = 5,
    ) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        if allowed_schema and not _SAFE_IDENTIFIER.fullmatch(allowed_schema):
            raise ValueError("allowed_schema must be a simple PostgreSQL identifier")
        if max_ctes < 0 or max_query_depth < 1:
            raise ValueError("query complexity limits are invalid")
        self.max_rows = max_rows
        self.allowed_schema = allowed_schema
        self.max_ctes = max_ctes
        self.max_query_depth = max_query_depth

    def prepare(self, sql: str) -> PreparedQuery:
        if not isinstance(sql, str) or not sql.strip():
            raise QueryPolicyError(QueryErrorCode.INVALID_SQL, "SQL must be a non-empty string")
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except Exception as exc:
            raise QueryPolicyError(QueryErrorCode.INVALID_SQL, "SQL could not be parsed") from exc

        if len(statements) != 1 or statements[0] is None:
            raise QueryPolicyError(
                QueryErrorCode.POLICY_DENIED,
                "exactly one SQL statement is required",
            )
        query = statements[0]
        if not isinstance(query, exp.Query):
            raise QueryPolicyError(
                QueryErrorCode.POLICY_DENIED,
                "only read-only SELECT queries are allowed",
            )
        if any(isinstance(node, _FORBIDDEN_EXPRESSIONS) for node in query.walk()):
            raise QueryPolicyError(
                QueryErrorCode.POLICY_DENIED,
                "write, DDL and server commands are not allowed",
            )
        if any(
            isinstance(node, exp.Query) and bool(node.args.get("locks"))
            for node in query.walk()
        ):
            raise QueryPolicyError(
                QueryErrorCode.POLICY_DENIED,
                "locking SELECT queries are not allowed",
            )

        self._validate_complexity(query)
        self._validate_functions(query)
        parameter_names = self._parameter_names(query)
        scoped_query, tables = self._enforce_schema(query.copy())
        bounded_query = self._apply_limit(scoped_query)
        bind_sql = _render_postgres_with_sqlalchemy_binds(bounded_query)
        safe_sql = str(text(bind_sql))

        return PreparedQuery(
            sql=safe_sql,
            bind_sql=bind_sql,
            fingerprint=_fingerprint(safe_sql),
            parameter_names=parameter_names,
            tables=tables,
            max_rows=self.max_rows,
            data_scope=(self.allowed_schema,) if self.allowed_schema else (),
        )

    def validate_params(
        self,
        prepared: PreparedQuery,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        bound = dict(params or {})
        if any(not isinstance(name, str) or not _SAFE_IDENTIFIER.fullmatch(name) for name in bound):
            raise QueryPolicyError(
                QueryErrorCode.PARAMETER_MISMATCH,
                "query parameter names must be simple identifiers",
            )
        expected = set(prepared.parameter_names)
        supplied = set(bound)
        if expected != supplied:
            missing = ", ".join(sorted(expected - supplied)) or "none"
            unexpected = ", ".join(sorted(supplied - expected)) or "none"
            raise QueryPolicyError(
                QueryErrorCode.PARAMETER_MISMATCH,
                f"query parameters do not match placeholders (missing: {missing}; unexpected: {unexpected})",
            )
        return bound

    def _validate_complexity(self, query: exp.Query) -> None:
        cte_count = sum(1 for _ in query.find_all(exp.CTE))
        if cte_count > self.max_ctes:
            raise QueryPolicyError(
                QueryErrorCode.POLICY_DENIED,
                f"query contains more than {self.max_ctes} CTEs",
            )
        if _query_depth(query) > self.max_query_depth:
            raise QueryPolicyError(
                QueryErrorCode.POLICY_DENIED,
                f"query nesting exceeds {self.max_query_depth} levels",
            )
        for join in query.find_all(exp.Join):
            is_cross = str(join.args.get("kind") or "").upper() == "CROSS"
            is_lateral = isinstance(join.this, exp.Lateral)
            has_constraint = any(
                join.args.get(key) is not None for key in ("on", "using", "method")
            )
            if (is_cross or not has_constraint) and not is_lateral:
                raise QueryPolicyError(
                    QueryErrorCode.POLICY_DENIED,
                    "cartesian joins are not allowed",
                )

    def _validate_functions(self, query: exp.Query) -> None:
        for function in query.find_all(exp.Func):
            name = _function_name(function)
            if name in _FORBIDDEN_FUNCTIONS or name.startswith(_FORBIDDEN_FUNCTION_PREFIXES):
                raise QueryPolicyError(
                    QueryErrorCode.POLICY_DENIED,
                    f"function {name} is not allowed",
                )
        for dot in query.find_all(exp.Dot):
            if not isinstance(dot.expression, exp.Func):
                continue
            namespace = dot.this
            in_system_catalog = _identifiers_equal(namespace, "pg_catalog")
            in_allowed_schema = bool(
                self.allowed_schema
                and _identifiers_equal(namespace, self.allowed_schema)
            )
            if not in_system_catalog and not in_allowed_schema:
                raise QueryPolicyError(
                    QueryErrorCode.SCHEMA_DENIED,
                    "query calls a function outside the allowed data scope",
                )

    def _parameter_names(self, query: exp.Query) -> tuple[str, ...]:
        if query.find(exp.Parameter) is not None:
            raise QueryPolicyError(
                QueryErrorCode.PARAMETER_MISMATCH,
                "only :name query parameters are allowed",
            )
        names: set[str] = set()
        for placeholder in query.find_all(exp.Placeholder):
            name = placeholder.name
            if not _SAFE_IDENTIFIER.fullmatch(name):
                raise QueryPolicyError(
                    QueryErrorCode.PARAMETER_MISMATCH,
                    "only :name query parameters are allowed",
                )
            names.add(name)
        return tuple(sorted(names))

    def _enforce_schema(self, query: exp.Query) -> tuple[exp.Query, tuple[str, ...]]:
        try:
            scopes = list(traverse_scope(query))
        except Exception as exc:
            raise QueryPolicyError(
                QueryErrorCode.INVALID_SQL,
                "SQL scope could not be resolved",
            ) from exc

        table_names: set[str] = set()
        for scope in scopes:
            for table in scope.tables:
                source = scope.sources.get(table.alias_or_name)
                if isinstance(source, Scope):
                    continue
                if table.catalog:
                    raise QueryPolicyError(
                        QueryErrorCode.SCHEMA_DENIED,
                        "cross-database references are not allowed",
                    )
                if table.db:
                    if not self.allowed_schema or not _identifiers_equal(
                        table.args.get("db"), self.allowed_schema
                    ):
                        raise QueryPolicyError(
                            QueryErrorCode.SCHEMA_DENIED,
                            "query references a schema outside the allowed data scope",
                        )
                elif table.name and self.allowed_schema:
                    table.set("db", exp.to_identifier(self.allowed_schema))
                if table.name:
                    table_names.add(table.sql(dialect="postgres"))
        return query, tuple(sorted(table_names))

    def _apply_limit(self, query: exp.Query) -> exp.Query:
        limit = query.args.get("limit")
        if limit is None:
            safe_limit = self.max_rows
        else:
            if isinstance(limit, exp.Fetch):
                literal = limit.args.get("count")
                options = limit.args.get("limit_options")
                if isinstance(options, exp.LimitOptions) and options.args.get("with_ties"):
                    raise QueryPolicyError(
                        QueryErrorCode.POLICY_DENIED,
                        "FETCH WITH TIES is not allowed",
                    )
            else:
                literal = limit.expression
            if not isinstance(literal, exp.Literal) or not literal.is_int:
                raise QueryPolicyError(
                    QueryErrorCode.POLICY_DENIED,
                    "LIMIT must be a non-negative integer literal",
                )
            requested = int(literal.this)
            if requested < 0:
                raise QueryPolicyError(
                    QueryErrorCode.POLICY_DENIED,
                    "LIMIT must be a non-negative integer literal",
                )
            safe_limit = min(requested, self.max_rows)
        return query.limit(safe_limit, copy=False)


class QueryGateway:
    """Execute policy-approved queries with database-enforced read-only limits."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        schema: str | None = None,
        timeout_seconds: int = 30,
        plan_timeout_ms: int = 3_000,
        lock_timeout_ms: int = 1_000,
        idle_timeout_ms: int = 10_000,
        max_rows: int = 200,
        max_plan_cost: float = 500_000.0,
        max_plan_rows: int = 100_000,
        max_result_bytes: int = 1_000_000,
        datasource: str = "business_postgres",
        readonly_role: str = "business_reader",
        audit_sink: QueryAuditSink | None = None,
    ) -> None:
        if schema and not _SAFE_IDENTIFIER.fullmatch(schema):
            raise ValueError("invalid schema identifier")
        if (
            timeout_seconds < 1
            or plan_timeout_ms < 1
            or lock_timeout_ms < 1
            or idle_timeout_ms < 1
        ):
            raise ValueError("query timeouts must be positive")
        if max_plan_cost < 0 or max_plan_rows < 0 or max_result_bytes < 2:
            raise ValueError("query resource limits are invalid")
        self._session_factory = session_factory
        self._schema = schema
        self._timeout_seconds = timeout_seconds
        self._plan_timeout_ms = plan_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        self._idle_timeout_ms = idle_timeout_ms
        self._max_rows = max_rows
        self._max_plan_cost = max_plan_cost
        self._max_plan_rows = max_plan_rows
        self._max_result_bytes = max_result_bytes
        self._datasource = datasource
        self._readonly_role = readonly_role
        self._audit_sink = audit_sink
        self._policy = PolicyEngine(max_rows=max_rows, allowed_schema=schema)

    def prepare(self, sql: str) -> PreparedQuery:
        return self._policy.prepare(sql)

    async def preflight(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> QueryReceipt:
        started = time.perf_counter()
        try:
            prepared = self.prepare(sql)
            bound = self._policy.validate_params(prepared, params)
        except QueryPolicyError as exc:
            return self._rejected(sql, exc.code, str(exc), started)
        try:
            cost, rows = await self._plan(prepared, bound)
        except QueryPolicyError as exc:
            return self._rejected_prepared(
                prepared,
                exc.code,
                str(exc),
                started,
                estimated_cost=exc.estimated_cost,
                estimated_rows=exc.estimated_rows,
            )
        except Exception as exc:
            return self._database_rejection(prepared, exc, started, stage="plan")
        return self._accepted_receipt(
            prepared,
            started,
            estimated_cost=cost,
            estimated_rows=rows,
        )

    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        trace_id: str = "",
    ) -> QueryReceipt:
        started = time.perf_counter()
        if self._audit_sink is not None:
            try:
                await self._audit_sink.record_sql_start(trace_id, sql)
            except Exception as exc:
                self._log_internal_failure("audit_start", exc, _fingerprint(sql))
                return self._rejected(
                    sql,
                    QueryErrorCode.AUDIT_UNAVAILABLE,
                    "required query audit is unavailable",
                    started,
                )

        from src.nl2sql.infra.governance.semaphore import get_concurrency_governor

        governor = get_concurrency_governor()
        async with governor.acquire("sql"):
            receipt = await self._execute_locked(sql, params, started=started)

        if self._audit_sink is not None:
            try:
                await self._audit_sink.record_sql_result(
                    trace_id,
                    sql,
                    accepted=receipt.accepted,
                    row_count=receipt.row_count,
                    elapsed_ms=receipt.elapsed_ms,
                    error_code=receipt.error.code if receipt.error else None,
                )
            except Exception as exc:
                self._log_internal_failure("audit_result", exc, receipt.sql_fingerprint)
                return self._rejected(
                    "",
                    QueryErrorCode.AUDIT_UNAVAILABLE,
                    "required query audit result could not be recorded",
                    started,
                    fingerprint=receipt.sql_fingerprint,
                    policy_outcome=receipt.policy_outcome,
                )
        return receipt

    async def _execute_locked(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        started: float | None = None,
    ) -> QueryReceipt:
        started = started if started is not None else time.perf_counter()
        try:
            prepared = self.prepare(sql)
            bound = self._policy.validate_params(prepared, params)
        except QueryPolicyError as exc:
            return self._rejected(sql, exc.code, str(exc), started)

        try:
            cost, rows_estimate, rows, masked_columns = await self._plan_and_run(
                prepared,
                bound,
            )
        except QueryPolicyError as exc:
            return self._rejected_prepared(
                prepared,
                exc.code,
                str(exc),
                started,
                estimated_cost=exc.estimated_cost,
                estimated_rows=exc.estimated_rows,
            )
        except _QueryDatabaseFailure as exc:
            return self._database_rejection(
                prepared,
                exc.error,
                started,
                stage=exc.stage,
                estimated_cost=exc.estimated_cost,
                estimated_rows=exc.estimated_rows,
            )

        return self._accepted_receipt(
            prepared,
            started,
            rows=rows,
            estimated_cost=cost,
            estimated_rows=rows_estimate,
            masked_columns=tuple(sorted(masked_columns)),
        )

    async def _plan(
        self,
        prepared: PreparedQuery,
        params: dict[str, Any],
    ) -> tuple[float, int]:
        async with self._session_factory() as session:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    await self._begin_read_only(session)
                    return await self._plan_with_timeout(session, prepared, params)
            finally:
                await session.rollback()

    async def _plan_and_run(
        self,
        prepared: PreparedQuery,
        params: dict[str, Any],
    ) -> tuple[float, int, list[dict[str, Any]], set[str]]:
        stage: Literal["plan", "execute"] = "plan"
        cost: float | None = None
        rows_estimate: int | None = None
        try:
            async with self._session_factory() as session:
                try:
                    async with asyncio.timeout(self._timeout_seconds):
                        await self._begin_read_only(session)
                        cost, rows_estimate = await self._plan_with_timeout(
                            session,
                            prepared,
                            params,
                        )
                        stage = "execute"
                        rows, masked_columns = await self._stream_in_session(
                            session,
                            prepared,
                            params,
                        )
                finally:
                    await session.rollback()
        except QueryPolicyError as exc:
            if exc.estimated_cost is None:
                exc.estimated_cost = cost
            if exc.estimated_rows is None:
                exc.estimated_rows = rows_estimate
            raise
        except Exception as exc:
            raise _QueryDatabaseFailure(
                stage,
                exc,
                estimated_cost=cost,
                estimated_rows=rows_estimate,
            ) from exc
        assert cost is not None and rows_estimate is not None
        return cost, rows_estimate, rows, masked_columns

    async def _plan_with_timeout(
        self,
        session: AsyncSession,
        prepared: PreparedQuery,
        params: dict[str, Any],
    ) -> tuple[float, int]:
        async with asyncio.timeout(self._plan_timeout_ms / 1000):
            return await self._plan_in_session(session, prepared, params)

    async def _plan_in_session(
        self,
        session: AsyncSession,
        prepared: PreparedQuery,
        params: dict[str, Any],
    ) -> tuple[float, int]:
        result = await session.execute(
            text(f"EXPLAIN (FORMAT JSON) {prepared.bind_sql}"),
            params,
        )
        row = result.fetchone()
        plan = _parse_plan(row[0] if row is not None else None)
        cost, rows = _plan_metrics(plan)
        if cost > self._max_plan_cost:
            raise QueryPolicyError(
                QueryErrorCode.COST_EXCEEDED,
                f"estimated cost {cost:.0f} exceeds policy",
                estimated_cost=cost,
                estimated_rows=rows,
            )
        if rows > self._max_plan_rows:
            raise QueryPolicyError(
                QueryErrorCode.ROWS_EXCEEDED,
                f"estimated rows {rows} exceeds policy",
                estimated_cost=cost,
                estimated_rows=rows,
            )
        return cost, rows

    async def _stream_in_session(
        self,
        session: AsyncSession,
        prepared: PreparedQuery,
        params: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        rows: list[dict[str, Any]] = []
        masked_columns: set[str] = set()
        encoded_size = 2  # JSON list brackets.
        result = await session.stream(text(prepared.bind_sql), params)
        keys = [str(key) for key in result.keys()]
        async for partition in result.partitions(32):
            for raw_row in partition:
                if len(rows) >= prepared.max_rows:
                    raise QueryPolicyError(
                        QueryErrorCode.ROWS_EXCEEDED,
                        "query returned more rows than the policy limit",
                    )
                safe_row, row_masked_columns = _mask_row(keys, raw_row)
                row_size = len(
                    json.dumps(
                        safe_row,
                        ensure_ascii=False,
                        default=str,
                    ).encode("utf-8")
                )
                encoded_size += row_size + (2 if rows else 0)
                if encoded_size > self._max_result_bytes:
                    raise QueryPolicyError(
                        QueryErrorCode.RESULT_TOO_LARGE,
                        f"query result exceeds {self._max_result_bytes} bytes",
                    )
                rows.append(safe_row)
                masked_columns.update(row_masked_columns)
        return rows, masked_columns

    async def _begin_read_only(self, session: AsyncSession) -> None:
        # This is deliberately the first statement on each fresh AsyncSession.
        # SQLAlchemy autobegin creates the transaction before PostgreSQL receives
        # SET TRANSACTION, so no query can run before read-only mode is asserted.
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(
            text(f"SET LOCAL statement_timeout = '{self._timeout_seconds * 1000}ms'")
        )
        await session.execute(text(f"SET LOCAL lock_timeout = '{self._lock_timeout_ms}ms'"))
        await session.execute(
            text(
                "SET LOCAL idle_in_transaction_session_timeout = "
                f"'{self._idle_timeout_ms}ms'"
            )
        )
        if self._schema:
            await session.execute(text(f"SET LOCAL search_path TO {self._schema}"))

    def _accepted_receipt(
        self,
        prepared: PreparedQuery,
        started: float,
        *,
        rows: list[dict[str, Any]] | None = None,
        estimated_cost: float | None = None,
        estimated_rows: int | None = None,
        masked_columns: tuple[str, ...] = (),
    ) -> QueryReceipt:
        result_rows = rows or []
        return QueryReceipt(
            accepted=True,
            sql=prepared.sql,
            sql_fingerprint=prepared.fingerprint,
            rows=result_rows,
            row_count=len(result_rows),
            estimated_cost=estimated_cost,
            estimated_rows=estimated_rows,
            elapsed_ms=_elapsed_ms(started),
            masked_columns=masked_columns,
            datasource=self._datasource,
            readonly_role=self._readonly_role,
            policy_outcome="allow",
            max_rows=self._max_rows,
            timeout_ms=self._timeout_seconds * 1000,
            data_scope=prepared.data_scope,
        )

    def _rejected_prepared(
        self,
        prepared: PreparedQuery,
        code: QueryErrorCode,
        message: str,
        started: float,
        *,
        estimated_cost: float | None = None,
        estimated_rows: int | None = None,
    ) -> QueryReceipt:
        return self._rejected(
            prepared.sql,
            code,
            message,
            started,
            fingerprint=prepared.fingerprint,
            policy_outcome="deny",
            data_scope=prepared.data_scope,
            estimated_cost=estimated_cost,
            estimated_rows=estimated_rows,
        )

    def _database_rejection(
        self,
        prepared: PreparedQuery,
        exc: Exception,
        started: float,
        *,
        stage: Literal["plan", "execute"],
        estimated_cost: float | None = None,
        estimated_rows: int | None = None,
    ) -> QueryReceipt:
        code, safe_message, retryable = _classify_database_error(exc, stage=stage)
        self._log_internal_failure(stage, exc, prepared.fingerprint)
        return self._rejected(
            prepared.sql,
            code,
            safe_message,
            started,
            retryable=retryable,
            fingerprint=prepared.fingerprint,
            policy_outcome="allow",
            data_scope=prepared.data_scope,
            estimated_cost=estimated_cost,
            estimated_rows=estimated_rows,
        )

    def _rejected(
        self,
        sql: str,
        code: QueryErrorCode,
        message: str,
        started: float,
        *,
        retryable: bool = False,
        fingerprint: str = "",
        policy_outcome: Literal["allow", "deny"] = "deny",
        data_scope: tuple[str, ...] = (),
        estimated_cost: float | None = None,
        estimated_rows: int | None = None,
    ) -> QueryReceipt:
        effective_scope = data_scope or ((self._schema,) if self._schema else ())
        return QueryReceipt(
            accepted=False,
            sql=sql,
            sql_fingerprint=fingerprint or _fingerprint(sql),
            estimated_cost=estimated_cost,
            estimated_rows=estimated_rows,
            elapsed_ms=_elapsed_ms(started),
            error=QueryError(code=code, message=message, retryable=retryable),
            datasource=self._datasource,
            readonly_role=self._readonly_role,
            policy_outcome=policy_outcome,
            max_rows=self._max_rows,
            timeout_ms=self._timeout_seconds * 1000,
            data_scope=effective_scope,
        )

    @staticmethod
    def _log_internal_failure(stage: str, exc: Exception, fingerprint: str) -> None:
        # Never interpolate the exception message: DBAPI errors can contain SQL,
        # literal values or connection details.  Class and SQLSTATE are enough
        # for operational classification while the caller gets a safe envelope.
        logger.warning(
            "query gateway failure stage=%s error_type=%s sqlstate=%s fingerprint=%s",
            stage,
            type(exc).__name__,
            _sqlstate(exc) or "unknown",
            fingerprint[:16],
        )


def _render_postgres_with_sqlalchemy_binds(query: exp.Query) -> str:
    """Render PostgreSQL syntax while retaining SQLAlchemy ``:name`` binds."""

    _escape_sqlalchemy_false_binds(query)
    if query.find(exp.Placeholder) is None:
        rendered = query.sql(dialect="postgres")
        _validate_rendered_binds(rendered, set())
        return rendered
    replacements: list[tuple[str, str]] = []
    nonce = uuid4().hex

    # sqlglot exposes the runtime base class but does not export its type in
    # the published Pyright stubs.  Keep the transform callback structurally
    # typed as Any at this integration boundary rather than importing a
    # private sqlglot type.
    def replace_placeholder(node: Any) -> Any:
        if not isinstance(node, exp.Placeholder):
            return node
        marker = f"{_PARAMETER_MARKER_PREFIX}{nonce}_{len(replacements)}__"
        replacements.append((marker, node.name))
        return exp.Var(this=marker)

    rendered = query.transform(replace_placeholder, copy=False).sql(dialect="postgres")
    for marker, name in replacements:
        if rendered.count(marker) != 1:
            raise QueryPolicyError(
                QueryErrorCode.INVALID_SQL,
                "query parameters could not be rendered safely",
            )
        rendered = rendered.replace(marker, f":{name}")
    _validate_rendered_binds(rendered, {name for _, name in replacements})
    return rendered


def _escape_sqlalchemy_false_binds(query: exp.Query) -> None:
    """Protect colons in SQL literals/comments from ``sqlalchemy.text`` parsing."""

    for node in query.walk():
        # PostgreSQL does not use optimizer hints, so comments can be removed
        # from executable SQL.  This also prevents ``-- :name`` from becoming a
        # synthetic SQLAlchemy bind.
        node.comments = []
        is_string_literal = isinstance(node, exp.Literal) and node.is_string
        is_other_string = isinstance(node, _SQLGLOT_STRING_EXPRESSIONS)
        is_quoted_identifier = isinstance(node, exp.Identifier) and bool(
            node.args.get("quoted")
        )
        if not (is_string_literal or is_other_string or is_quoted_identifier):
            continue
        value = node.this
        if isinstance(value, str):
            node.set("this", _SQLALCHEMY_FALSE_BIND.sub(r"\\:", value))


def _validate_rendered_binds(rendered: str, expected: set[str]) -> None:
    detected = set(text(rendered)._bindparams)
    if detected != expected:
        raise QueryPolicyError(
            QueryErrorCode.INVALID_SQL,
            "query parameters could not be rendered safely",
        )


def _query_depth(query: exp.Query) -> int:
    maximum = 0
    for node in query.walk():
        if not isinstance(node, exp.Query):
            continue
        depth = 1
        parent = node.parent
        while parent is not None:
            if isinstance(parent, exp.Query):
                depth += 1
            parent = parent.parent
        maximum = max(maximum, depth)
    return maximum


def _function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return function.name.lower()
    return function.sql_name().lower()


def _identifiers_equal(identifier: Any | None, allowed: str) -> bool:
    if not isinstance(identifier, exp.Identifier):
        return False
    # sqlglot's dynamically generated expression stubs do not narrow this
    # subclass reliably under Pyright, although the runtime isinstance check
    # above is definitive.
    safe_identifier: Any = identifier
    actual = str(safe_identifier.name)
    if safe_identifier.args.get("quoted"):
        return actual == allowed
    return actual.lower() == allowed.lower()


def _parse_plan(raw_plan: Any) -> dict[str, Any]:
    if isinstance(raw_plan, str):
        raw_plan = json.loads(raw_plan)
    if isinstance(raw_plan, list) and len(raw_plan) == 1 and isinstance(raw_plan[0], dict):
        plan = raw_plan[0].get("Plan")
    elif isinstance(raw_plan, dict):
        plan = raw_plan.get("Plan")
    else:
        plan = None
    if not isinstance(plan, dict):
        raise ValueError("EXPLAIN returned an invalid plan")
    return plan


def _plan_metrics(plan: dict[str, Any]) -> tuple[float, int]:
    total_cost = _non_negative_number(plan.get("Total Cost"), "Total Cost")
    max_rows = int(_non_negative_number(plan.get("Plan Rows"), "Plan Rows"))
    pending = list(plan.get("Plans", []))
    if "Plans" in plan and not isinstance(plan["Plans"], list):
        raise ValueError("EXPLAIN returned malformed child plans")
    while pending:
        child = pending.pop()
        if not isinstance(child, dict):
            raise ValueError("EXPLAIN returned malformed child plans")
        if "Plan Rows" in child:
            max_rows = max(
                max_rows,
                int(_non_negative_number(child["Plan Rows"], "Plan Rows")),
            )
        grandchildren = child.get("Plans", [])
        if not isinstance(grandchildren, list):
            raise ValueError("EXPLAIN returned malformed child plans")
        pending.extend(grandchildren)
    return total_cost, max_rows


def _non_negative_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"EXPLAIN omitted {field_name}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"EXPLAIN returned invalid {field_name}")
    return number


def _mask_row(keys: list[str], raw_row: Any) -> tuple[dict[str, Any], set[str]]:
    row = dict(zip(keys, raw_row, strict=False))
    masked_columns: set[str] = set()
    for key in tuple(row):
        if _SENSITIVE_COLUMN.search(key):
            row[key] = "[REDACTED]"
            masked_columns.add(key)
    return row, masked_columns


def _mask_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
    masked_columns: set[str] = set()
    safe_rows: list[dict[str, Any]] = []
    for row in rows:
        safe_row, row_masked = _mask_row(list(row), list(row.values()))
        safe_rows.append(safe_row)
        masked_columns.update(row_masked)
    return safe_rows, masked_columns


def _classify_database_error(
    exc: Exception,
    *,
    stage: Literal["plan", "execute"],
) -> tuple[QueryErrorCode, str, bool]:
    if isinstance(exc, TimeoutError):
        return QueryErrorCode.TIMEOUT, f"query {stage} timed out", True
    state = _sqlstate(exc)
    if state == "57014":
        return QueryErrorCode.TIMEOUT, f"query {stage} timed out", True
    if state is not None and state.startswith("08"):
        return QueryErrorCode.CONNECTION_ERROR, "database connection failed", True
    if state in {"40001", "40P01", "53300", "55P03", "57P01", "57P02", "57P03"}:
        return (
            QueryErrorCode.TRANSIENT_DATABASE_ERROR,
            "database temporarily could not execute the query",
            True,
        )
    if bool(getattr(exc, "connection_invalidated", False)):
        return QueryErrorCode.CONNECTION_ERROR, "database connection failed", True
    if state in {"25006", "42501"}:
        return QueryErrorCode.PERMISSION_DENIED, "database permission denied", False
    if state == "42P18" or (state is not None and state.startswith("22")):
        return (
            QueryErrorCode.PARAMETER_INVALID,
            "query parameter value or type is invalid",
            False,
        )
    if state in {"42P01", "42703"}:
        return QueryErrorCode.RELATION_NOT_FOUND, "requested relation or column was not found", False
    if stage == "plan":
        return QueryErrorCode.PLAN_FAILED, "query planning failed", False
    return QueryErrorCode.DATABASE_ERROR, "query execution failed", False


def _sqlstate(exc: Exception) -> str | None:
    pending: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(current, attribute, None)
            if isinstance(value, str) and value:
                return value
        pending.extend(
            [
                getattr(current, "orig", None),
                current.__cause__,
                current.__context__,
            ]
        )
    return None


def _fingerprint(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8", errors="replace")).hexdigest()


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000)
