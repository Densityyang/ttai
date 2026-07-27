"""Tests for Phase 4 enhanced SQL Guard -- AST validation, Cartesian product, forbidden keywords."""


from src.nl2sql.infra.governance.sql_guard import (
    check_cte_depth,
    check_forbidden_keywords,
    check_forbidden_statements,
    check_subquery_depth,
    detect_cartesian_product,
    enhanced_validate_query,
    inject_limit,
)

# ── AST 禁止语句检测 ──────────────────────────────────────────────────────────


class TestForbiddenStatements:
    def test_select_allowed(self) -> None:
        assert check_forbidden_statements("SELECT * FROM orders") is None

    def test_with_cte_allowed(self) -> None:
        sql = "WITH cte AS (SELECT 1) SELECT * FROM cte"
        assert check_forbidden_statements(sql) is None

    def test_insert_blocked(self) -> None:
        err = check_forbidden_statements("INSERT INTO orders VALUES (1, 'test')")
        assert err is not None
        assert "INSERT" in err

    def test_update_blocked(self) -> None:
        err = check_forbidden_statements("UPDATE orders SET status = 'done'")
        assert err is not None

    def test_delete_blocked(self) -> None:
        err = check_forbidden_statements("DELETE FROM orders WHERE id = 1")
        assert err is not None

    def test_drop_blocked(self) -> None:
        err = check_forbidden_statements("DROP TABLE orders")
        assert err is not None

    def test_alter_blocked(self) -> None:
        err = check_forbidden_statements("ALTER TABLE orders ADD COLUMN x INT")
        assert err is not None

    def test_truncate_blocked(self) -> None:
        err = check_forbidden_statements("TRUNCATE TABLE orders")
        assert err is not None

    def test_grant_blocked(self) -> None:
        err = check_forbidden_statements("GRANT SELECT ON orders TO user1")
        assert err is not None

    def test_keyword_in_string_literal_allowed(self) -> None:
        """DELETE inside a string literal should NOT be blocked."""
        sql = "SELECT * FROM orders WHERE status = 'DELETE_PENDING'"
        result = check_forbidden_statements(sql)
        assert result is None


# ── 禁止关键词模式检测 ────────────────────────────────────────────────────────


class TestForbiddenKeywords:
    def test_into_outfile_blocked(self) -> None:
        err = check_forbidden_keywords("SELECT * FROM t INTO OUTFILE '/tmp/x'")
        assert err is not None
        assert "INTO OUTFILE" in err

    def test_load_data_blocked(self) -> None:
        err = check_forbidden_keywords("LOAD DATA INFILE '/tmp/x' INTO TABLE t")
        assert err is not None
        assert "LOAD DATA" in err

    def test_copy_to_blocked(self) -> None:
        err = check_forbidden_keywords("COPY orders TO '/tmp/out.csv'")
        assert err is not None

    def test_normal_select_allowed(self) -> None:
        assert check_forbidden_keywords("SELECT count(*) FROM orders") is None


# ── 笛卡尔积检测 ──────────────────────────────────────────────────────────────


class TestCartesianProduct:
    def test_single_table_allowed(self) -> None:
        assert detect_cartesian_product("SELECT * FROM orders") is None

    def test_join_with_on_allowed(self) -> None:
        sql = "SELECT * FROM orders JOIN users ON orders.user_id = users.id"
        assert detect_cartesian_product(sql) is None

    def test_multi_table_with_where_allowed(self) -> None:
        sql = "SELECT * FROM orders, users WHERE orders.user_id = users.id"
        assert detect_cartesian_product(sql) is None

    def test_multi_table_no_condition_blocked(self) -> None:
        sql = "SELECT * FROM orders, users, products"
        err = detect_cartesian_product(sql)
        assert err is not None
        assert "笛卡尔积" in err


# ── LIMIT 注入 ────────────────────────────────────────────────────────────────


class TestLimitInjection:
    def test_adds_limit(self) -> None:
        result = inject_limit("SELECT * FROM orders")
        assert "LIMIT" in result

    def test_preserves_existing_limit(self) -> None:
        sql = "SELECT * FROM orders LIMIT 10"
        result = inject_limit(sql)
        assert result == sql

    def test_non_select_unchanged(self) -> None:
        sql = "EXPLAIN SELECT * FROM orders"
        result = inject_limit(sql)
        assert result == sql


# ── CTE/子查询深度 ────────────────────────────────────────────────────────────


class TestDepthChecks:
    def test_normal_cte_allowed(self) -> None:
        assert check_cte_depth("WITH a AS (SELECT 1) SELECT * FROM a") is None

    def test_deep_cte_blocked(self) -> None:
        sql = " ".join(["WITH"] * 10) + " a AS (SELECT 1) SELECT * FROM a"
        err = check_cte_depth(sql, max_depth=5)
        assert err is not None

    def test_deep_subquery_blocked(self) -> None:
        sql = "SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM (SELECT * FROM t))))"
        err = check_subquery_depth(sql, max_depth=3)
        assert err is not None


# ── 统一入口 ──────────────────────────────────────────────────────────────────


class TestEnhancedValidate:
    def test_safe_select(self) -> None:
        safe_sql, err = enhanced_validate_query("SELECT id, name FROM users WHERE age > 18")
        assert err is None
        assert "LIMIT" in safe_sql

    def test_insert_blocked(self) -> None:
        _, err = enhanced_validate_query("INSERT INTO users VALUES (1)")
        assert err is not None

    def test_cartesian_blocked(self) -> None:
        _, err = enhanced_validate_query("SELECT * FROM a, b, c")
        assert err is not None

    def test_into_outfile_blocked(self) -> None:
        _, err = enhanced_validate_query("SELECT * FROM t INTO OUTFILE '/tmp/x'")
        assert err is not None
