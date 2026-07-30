"""Compatibility helpers backed by the canonical :mod:`query_gateway` policy.

This module intentionally owns no database execution logic.  Older imports are
kept for source compatibility, while all policy and EXPLAIN decisions delegate
to ``PolicyEngine`` or ``QueryGateway``.  New application code must call
``QueryGateway`` directly.
"""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

from src.nl2sql.infra.governance.query_gateway import (
    PolicyEngine,
    QueryGateway,
    QueryPolicyError,
)

DEFAULT_MAX_ROWS = 5_000
MAX_CTE_DEPTH = 5
MAX_SUBQUERY_DEPTH = 4
EXPLAIN_COST_THRESHOLD = 500_000.0

_FORBIDDEN_KEYWORD_PATTERNS = (
    re.compile(r"\bINTO\s+OUTFILE\b", re.IGNORECASE),
    re.compile(r"\bLOAD\s+DATA\b", re.IGNORECASE),
    re.compile(r"\bINTO\s+DUMPFILE\b", re.IGNORECASE),
    re.compile(r"\bCOPY\b[\s\S]*?\b(?:TO|FROM)\b", re.IGNORECASE),
)


def inject_limit(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """Return canonical bounded SQL and raise when the statement is not safe."""

    return PolicyEngine(max_rows=max_rows).prepare(sql).sql


def check_cte_depth(sql: str, max_depth: int = MAX_CTE_DEPTH) -> str | None:
    """Report when the parsed query contains too many CTE definitions."""

    query = _parse_query(sql)
    if query is None:
        return "SQL could not be parsed"
    count = sum(1 for _ in query.find_all(exp.CTE))
    if count > max_depth:
        return f"CTE count ({count}) exceeds limit ({max_depth})"
    return None


def check_subquery_depth(sql: str, max_depth: int = MAX_SUBQUERY_DEPTH) -> str | None:
    """Report when nested query expressions exceed the compatibility limit."""

    query = _parse_query(sql)
    if query is None:
        return "SQL could not be parsed"
    depth = _query_depth(query) - 1
    if depth > max_depth:
        return f"subquery depth ({depth}) exceeds limit ({max_depth})"
    return None


def check_forbidden_statements(sql: str) -> str | None:
    """Return the canonical policy error for non-read-only statements."""

    try:
        statement = sqlglot.parse_one(sql, read="postgres")
    except Exception:
        statement = None
    if statement is not None and not isinstance(statement, exp.Query):
        return f"forbidden SQL statement: {statement.key.upper()}"
    try:
        PolicyEngine(max_rows=DEFAULT_MAX_ROWS).prepare(sql)
    except QueryPolicyError as exc:
        return str(exc)
    return None


def check_forbidden_keywords(sql: str) -> str | None:
    """Retain the old diagnostic helper without creating an execution path."""

    for pattern in _FORBIDDEN_KEYWORD_PATTERNS:
        match = pattern.search(sql)
        if match:
            return f"forbidden operation: {match.group()}"
    return None


def detect_cartesian_product(sql: str) -> str | None:
    """Detect comma and explicit CROSS joins using SQLGlot rather than text tokens."""

    query = _parse_query(sql)
    if query is None:
        return "SQL could not be parsed"
    for join in query.find_all(exp.Join):
        is_cross = str(join.args.get("kind") or "").upper() == "CROSS"
        is_comma = not any(
            join.args.get(key) is not None for key in ("on", "using", "method", "side")
        )
        if is_cross or (is_comma and query.find(exp.Where) is None):
            return "cartesian product is not allowed"
    return None


async def estimate_query_cost(
    sql: str,
    session_factory: Any,
    schema: str | None = None,
    cost_threshold: float = EXPLAIN_COST_THRESHOLD,
    params: dict[str, Any] | None = None,
) -> tuple[float, str | None]:
    """Compatibility EXPLAIN helper that fails closed through ``QueryGateway``."""

    gateway = QueryGateway(
        session_factory,
        schema=schema,
        max_rows=DEFAULT_MAX_ROWS,
        max_plan_cost=cost_threshold,
        max_plan_rows=2**63 - 1,
    )
    receipt = await gateway.preflight(sql, params)
    if not receipt.accepted:
        assert receipt.error is not None
        return receipt.estimated_cost or float("inf"), receipt.error.message
    if receipt.estimated_cost is None:
        return float("inf"), "query planning returned no cost"
    return receipt.estimated_cost, None


def enhanced_validate_query(sql: str) -> tuple[str, str | None]:
    """Compatibility policy entry point delegated to ``PolicyEngine``."""

    try:
        prepared = PolicyEngine(max_rows=DEFAULT_MAX_ROWS).prepare(sql)
    except QueryPolicyError as exc:
        return "", str(exc)
    return prepared.sql, None


async def full_validate_query(
    sql: str,
    session_factory: Any | None = None,
    schema: str | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Compatibility full validation; EXPLAIN is always fail-closed when requested."""

    if session_factory is None:
        try:
            prepared = PolicyEngine(
                max_rows=DEFAULT_MAX_ROWS,
                allowed_schema=schema,
            ).prepare(sql)
        except QueryPolicyError as exc:
            return "", str(exc)
        return prepared.sql, None

    gateway = QueryGateway(
        session_factory,
        schema=schema,
        max_rows=DEFAULT_MAX_ROWS,
    )
    receipt = await gateway.preflight(sql, params)
    if not receipt.accepted:
        assert receipt.error is not None
        return "", receipt.error.message
    return receipt.sql, None


def _parse_query(sql: str) -> exp.Query | None:
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except Exception:
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return None
    return statements[0]


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
