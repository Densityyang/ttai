"""SQL 安全守卫（Phase 4 全面增强版）。

在原有 LIMIT 注入、CTE/子查询深度检查基础上，新增：
- sqlparse AST 全面校验：禁止 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE 等写操作
- 笛卡尔积检测：无 JOIN 条件的多表查询拦截
- INTO OUTFILE / LOAD DATA 拦截
- EXPLAIN 查询成本估算：超阈值拒绝

并发安全说明：
- 所有函数为纯函数（无共享可变状态），天然并发安全
- EXPLAIN 需要数据库连接，但每次调用使用独立 session
"""

import logging
import re
from typing import Any

import sqlparse
from sqlparse.sql import (
    Identifier,
    IdentifierList,
)
from sqlparse.tokens import DDL, DML, Keyword

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 5000
MAX_CTE_DEPTH = 5
MAX_SUBQUERY_DEPTH = 4

# TUNABLE: EXPLAIN 成本阈值。超过此值认为查询代价过高，拒绝执行。
# 该值对应 PostgreSQL EXPLAIN 输出的 total_cost（plan rows * width 的估算）。
# 需要根据实际数据规模和硬件性能调整。
EXPLAIN_COST_THRESHOLD: float = 500_000.0  # TUNABLE: 可能需要根据实际场景调整

# 禁止的 DML/DDL 语句类型（大写）
_FORBIDDEN_STATEMENT_TYPES: set[str] = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER",
    "TRUNCATE", "GRANT", "REVOKE", "CREATE", "REPLACE",
}

# 禁止的关键词模式
_FORBIDDEN_KEYWORD_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bINTO\s+OUTFILE\b", re.IGNORECASE),
    re.compile(r"\bLOAD\s+DATA\b", re.IGNORECASE),
    re.compile(r"\bINTO\s+DUMPFILE\b", re.IGNORECASE),
    re.compile(r"\bCOPY\b[\s\S]*?\bTO\b", re.IGNORECASE),
    re.compile(r"\bCOPY\b[\s\S]*?\bFROM\b", re.IGNORECASE),
]


def inject_limit(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """为没有 LIMIT 子句的 SELECT 语句自动注入 LIMIT。"""
    stripped = sql.strip().rstrip(";")
    upper = stripped.upper()

    if "LIMIT" in upper:
        return sql

    if not upper.startswith("SELECT") and not upper.startswith("WITH"):
        return sql

    return f"{stripped}\nLIMIT {max_rows}"


def check_cte_depth(sql: str, max_depth: int = MAX_CTE_DEPTH) -> str | None:
    """检查 CTE (WITH) 嵌套深度。"""
    upper = sql.upper()
    with_count = len(re.findall(r"\bWITH\b", upper))
    if with_count > max_depth:
        return f"CTE 嵌套深度 ({with_count}) 超过限制 ({max_depth})"
    return None


def check_subquery_depth(sql: str, max_depth: int = MAX_SUBQUERY_DEPTH) -> str | None:
    """检查子查询嵌套深度。"""
    upper = sql.upper()
    select_in_parens = len(re.findall(r"\(\s*SELECT\b", upper))
    if select_in_parens > max_depth:
        return f"子查询嵌套层数 ({select_in_parens}) 超过限制 ({max_depth})"
    return None


# ── Phase 4 新增：AST 全面校验 ──────────────────────────────────────────────────


def check_forbidden_statements(sql: str) -> str | None:
    """基于 sqlparse AST 检查是否包含禁止的 SQL 语句类型。

    比纯正则更可靠：可以正确处理注释、字符串字面量中的关键词。

    Returns:
        错误信息（如果检测到禁止语句），否则 None。
    """
    try:
        parsed = sqlparse.parse(sql)
    except Exception as e:
        return f"SQL 解析失败: {e}"

    for statement in parsed:
        stmt_type = statement.get_type()
        if stmt_type and stmt_type.upper() in _FORBIDDEN_STATEMENT_TYPES:
            return f"禁止的 SQL 语句类型: {stmt_type}"

        for token in statement.flatten():
            if token.normalized.upper() in _FORBIDDEN_STATEMENT_TYPES:
                return f"禁止的 SQL 操作: {token.normalized}"
            if token.ttype is DML and token.normalized.upper() in _FORBIDDEN_STATEMENT_TYPES:
                return f"禁止的 DML 操作: {token.normalized}"
            if token.ttype is DDL:
                return f"禁止的 DDL 操作: {token.normalized}"

    return None


def check_forbidden_keywords(sql: str) -> str | None:
    """检测禁止的关键词模式（INTO OUTFILE / LOAD DATA 等）。

    Returns:
        错误信息（如果命中），否则 None。
    """
    for pattern in _FORBIDDEN_KEYWORD_PATTERNS:
        match = pattern.search(sql)
        if match:
            return f"禁止的操作: {match.group()}"
    return None


def detect_cartesian_product(sql: str) -> str | None:
    """检测笛卡尔积：FROM 子句中有多张表但无 JOIN/WHERE 条件关联。

    策略：解析 FROM 子句中的表数量，若 > 1 且无 WHERE/JOIN 条件则告警。
    采用保守策略：只对明显的多表无条件查询拦截。

    Returns:
        警告信息（如果检测到），否则 None。
    """
    try:
        parsed = sqlparse.parse(sql)
    except Exception:
        return None

    for statement in parsed:
        from_tables = _extract_from_tables(statement)
        if len(from_tables) <= 1:
            continue

        has_join = bool(re.search(r"\bJOIN\b", sql, re.IGNORECASE))
        has_where = bool(re.search(r"\bWHERE\b", sql, re.IGNORECASE))
        has_on = bool(re.search(r"\bON\b", sql, re.IGNORECASE))

        if not has_join and not has_where and not has_on:
            tables_str = ", ".join(from_tables)
            return (
                f"检测到潜在笛卡尔积：FROM 子句包含 {len(from_tables)} 张表 "
                f"({tables_str}) 但无 JOIN/WHERE/ON 条件"
            )

    return None


def _extract_from_tables(statement: Any) -> list[str]:
    """从 sqlparse Statement 中提取 FROM 子句的表名。"""
    tables: list[str] = []
    from_seen = False

    for token in statement.tokens:
        if token.ttype is Keyword and token.normalized.upper() == "FROM":
            from_seen = True
            continue

        if from_seen:
            if isinstance(token, IdentifierList):
                for identifier in token.get_identifiers():
                    name = _get_table_name(identifier)
                    if name:
                        tables.append(name)
                from_seen = False
            elif isinstance(token, Identifier):
                name = _get_table_name(token)
                if name:
                    tables.append(name)
                from_seen = False
            elif token.ttype is Keyword and token.normalized.upper() in (
                "WHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "UNION",
                "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS",
            ):
                from_seen = False

    return tables


def _get_table_name(identifier: Any) -> str | None:
    """从 sqlparse Identifier 中提取表名。"""
    if isinstance(identifier, Identifier):
        real_name = identifier.get_real_name()
        return str(real_name) if real_name else None
    name = str(identifier).strip()
    if name and not name.startswith("("):
        return name.split()[0] if " " in name else name
    return None


# ── Phase 4 新增：EXPLAIN 成本估算 ──────────────────────────────────────────────


async def estimate_query_cost(
    sql: str,
    session_factory: Any,
    schema: str | None = None,
    cost_threshold: float = EXPLAIN_COST_THRESHOLD,
) -> tuple[float, str | None]:
    """通过 EXPLAIN 预估查询成本。

    Args:
        sql: 要评估的 SQL 查询
        session_factory: SQLAlchemy async session factory
        schema: 可选 search_path schema
        cost_threshold: 成本阈值

    Returns:
        (estimated_cost, error_message_or_none)
        如果成本超过阈值，error_message 非 None。
    """
    from sqlalchemy import text

    explain_sql = f"EXPLAIN (FORMAT JSON) {sql.rstrip(';')}"

    try:
        async with session_factory() as session:
            if schema:
                await session.execute(text(f"SET LOCAL search_path TO {schema}"))
            result = await session.execute(text(explain_sql))
            row = result.fetchone()
            if row is None:
                return 0.0, None

            import json
            plan_data = row[0]
            if isinstance(plan_data, str):
                plan_data = json.loads(plan_data)

            if isinstance(plan_data, list) and plan_data:
                plan = plan_data[0].get("Plan", {})
            elif isinstance(plan_data, dict):
                plan = plan_data.get("Plan", {})
            else:
                return 0.0, None

            total_cost = float(plan.get("Total Cost", 0.0))

            if total_cost > cost_threshold:
                return total_cost, (
                    f"查询预估成本过高: {total_cost:.0f} > 阈值 {cost_threshold:.0f}。"
                    f"请优化查询或添加更精确的筛选条件。"
                )

            logger.debug("EXPLAIN 成本估算: %.0f (阈值: %.0f)", total_cost, cost_threshold)
            return total_cost, None

    except Exception as e:
        logger.warning("EXPLAIN 成本估算失败 (非阻断): %s", e)
        return 0.0, None


# ── 统一入口 ────────────────────────────────────────────────────────────────────


def enhanced_validate_query(sql: str) -> tuple[str, str | None]:
    """增强的 SQL 安全验证（同步部分，不含 EXPLAIN 成本估算）。

    Phase 4 增强：增加了 AST 校验、笛卡尔积检测、关键词拦截。

    Returns:
        (处理后的 SQL, 错误信息或 None)
    """
    # 1. AST 禁止语句检查（最优先，最严格）
    stmt_err = check_forbidden_statements(sql)
    if stmt_err:
        return sql, stmt_err

    # 2. 禁止关键词模式检查
    kw_err = check_forbidden_keywords(sql)
    if kw_err:
        return sql, kw_err

    # 3. CTE 深度检查
    cte_err = check_cte_depth(sql)
    if cte_err:
        return sql, cte_err

    # 4. 子查询深度检查
    sub_err = check_subquery_depth(sql)
    if sub_err:
        return sql, sub_err

    # 5. 笛卡尔积检测（警告级，但仍拦截）
    cartesian_err = detect_cartesian_product(sql)
    if cartesian_err:
        return sql, cartesian_err

    # 6. 自动 LIMIT 注入
    safe_sql = inject_limit(sql)
    return safe_sql, None


async def full_validate_query(
    sql: str,
    session_factory: Any | None = None,
    schema: str | None = None,
) -> tuple[str, str | None]:
    """完整 SQL 安全验证（含 EXPLAIN 成本估算）。

    Phase 4 增强入口：先做同步校验，再做异步成本估算。

    Args:
        sql: 待验证的 SQL
        session_factory: SQLAlchemy async session factory（可选）
        schema: search_path schema（可选）

    Returns:
        (处理后的 SQL, 错误信息或 None)
    """
    safe_sql, sync_err = enhanced_validate_query(sql)
    if sync_err:
        return safe_sql, sync_err

    if session_factory is not None:
        _, cost_err = await estimate_query_cost(
            safe_sql, session_factory, schema=schema,
        )
        if cost_err:
            return safe_sql, cost_err

    return safe_sql, None
