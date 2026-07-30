"""Compatibility tests for the legacy SQL guard facade."""

from __future__ import annotations

import inspect

import pytest

from src.nl2sql.infra.governance import sql_guard
from src.nl2sql.infra.governance.query_gateway import QueryPolicyError
from src.nl2sql.infra.governance.sql_guard import (
    check_cte_depth,
    check_forbidden_keywords,
    check_forbidden_statements,
    check_subquery_depth,
    detect_cartesian_product,
    enhanced_validate_query,
    full_validate_query,
    inject_limit,
)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders VALUES (1)",
        "UPDATE orders SET status = 'done'",
        "DELETE FROM orders",
        "DROP TABLE orders",
        "ALTER TABLE orders ADD COLUMN x INT",
        "TRUNCATE TABLE orders",
        "GRANT SELECT ON orders TO user1",
    ],
)
def test_forbidden_statements_delegate_to_canonical_policy(sql: str) -> None:
    assert check_forbidden_statements(sql) is not None


def test_keywords_in_literals_are_allowed() -> None:
    assert check_forbidden_statements(
        "SELECT * FROM orders WHERE status = 'DELETE_PENDING'"
    ) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t INTO OUTFILE '/tmp/x'",
        "LOAD DATA INFILE '/tmp/x' INTO TABLE t",
        "COPY orders TO '/tmp/out.csv'",
    ],
)
def test_forbidden_keyword_diagnostics_are_retained(sql: str) -> None:
    assert check_forbidden_keywords(sql) is not None


def test_cartesian_diagnostic_uses_ast() -> None:
    assert detect_cartesian_product("SELECT * FROM orders") is None
    assert (
        detect_cartesian_product(
            "SELECT * FROM orders JOIN users ON orders.user_id = users.id"
        )
        is None
    )
    assert (
        detect_cartesian_product(
            "SELECT * FROM orders, users WHERE orders.user_id = users.id"
        )
        is None
    )
    assert detect_cartesian_product("SELECT * FROM orders, users") is not None


def test_limit_compatibility_helper_uses_canonical_rewrite() -> None:
    assert inject_limit("SELECT * FROM orders").endswith("LIMIT 5000")
    assert inject_limit("SELECT * FROM orders LIMIT 10").endswith("LIMIT 10")
    with pytest.raises(QueryPolicyError):
        inject_limit("EXPLAIN SELECT * FROM orders")


def test_depth_diagnostics_use_parsed_query() -> None:
    assert check_cte_depth("WITH a AS (SELECT 1) SELECT * FROM a") is None
    assert (
        check_cte_depth(
            "WITH a AS (SELECT 1), b AS (SELECT 1), c AS (SELECT 1) SELECT * FROM a",
            max_depth=2,
        )
        is not None
    )
    assert (
        check_subquery_depth(
            "SELECT * FROM (SELECT * FROM (SELECT * FROM t) b) a",
            max_depth=1,
        )
        is not None
    )


def test_enhanced_validation_is_a_policy_engine_facade() -> None:
    safe_sql, error = enhanced_validate_query(
        "SELECT id, name FROM users WHERE age > 18"
    )
    assert error is None
    assert safe_sql.endswith("LIMIT 5000")

    for unsafe in (
        "INSERT INTO users VALUES (1)",
        "SELECT * FROM a, b",
        "SELECT * FROM t INTO OUTFILE '/tmp/x'",
    ):
        _, error = enhanced_validate_query(unsafe)
        assert error is not None


@pytest.mark.asyncio
async def test_full_validation_without_database_is_policy_only() -> None:
    safe_sql, error = await full_validate_query(
        "SELECT * FROM orders",
        schema="ai_views",
    )

    assert error is None
    assert safe_sql == "SELECT * FROM ai_views.orders LIMIT 5000"


def test_legacy_module_has_no_database_execution_implementation() -> None:
    source = inspect.getsource(sql_guard)

    assert "session.execute" not in source
    assert "EXPLAIN (FORMAT JSON)" not in source
    assert "QueryGateway(" in source
