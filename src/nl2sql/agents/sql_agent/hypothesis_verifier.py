"""假设验证器 -- APEX-SQL 风格的数据画像。

在 SQL 生成前对候选表/列执行轻量级验证查询，
减少"幻觉列/幻觉值"问题。

验证策略：
1. 列存在性验证：SELECT column_name FROM information_schema.columns
2. 值域采样：SELECT DISTINCT col LIMIT N
3. 数据存在性：SELECT COUNT(*) WHERE ...
"""

import asyncio
import logging
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool

from src.nl2sql.infra.governance.semaphore import get_concurrency_governor

logger = logging.getLogger(__name__)

MAX_DISTINCT_SAMPLES = 10
MAX_CONCURRENT_PROBES = 3


@dataclass
class ColumnProfile:
    """单列的数据画像。"""

    table: str
    column: str
    exists: bool = False
    distinct_values: list[str] = field(default_factory=list)
    sample_count: int = 0
    data_type: str = ""
    error: str = ""


@dataclass
class TableProfile:
    """单表的数据画像。"""

    table: str
    exists: bool = False
    row_count: int = 0
    columns: list[ColumnProfile] = field(default_factory=list)
    error: str = ""


@dataclass
class HypothesisReport:
    """假设验证报告 -- 作为补充上下文注入 SQL 生成提示。"""

    tables: list[TableProfile]
    warnings: list[str] = field(default_factory=list)
    verified: bool = False

    def to_context_str(self) -> str:
        """生成可注入提示词的验证上下文文本。"""
        if not self.tables:
            return ""

        parts = ["【数据画像验证结果】\n"]
        for tp in self.tables:
            if not tp.exists:
                parts.append(f"⚠ 表 {tp.table} 不存在或无权限访问")
                continue

            parts.append(f"✓ 表 {tp.table} (约 {tp.row_count} 行)")
            for cp in tp.columns:
                if not cp.exists:
                    parts.append(f"  ✗ 列 {cp.column} 不存在于 {cp.table}")
                    continue
                vals = ", ".join(cp.distinct_values[:5]) if cp.distinct_values else "（未采样）"
                parts.append(f"  ✓ {cp.column} [{cp.data_type}] 样本值: {vals}")

        if self.warnings:
            parts.append("\n注意:")
            for w in self.warnings:
                parts.append(f"  - {w}")

        return "\n".join(parts)


async def verify_hypotheses(
    candidate_tables: list[str],
    candidate_columns: dict[str, list[str]],
    tools: dict[str, BaseTool],
    schema_name: str = "ai_views",
) -> HypothesisReport:
    """对候选表和列执行假设验证。

    Args:
        candidate_tables: 候选表名列表
        candidate_columns: 表名 -> 候选列名列表
        tools: SQL 工具集
        schema_name: 数据库 schema 名

    Returns:
        HypothesisReport 验证报告
    """
    if not candidate_tables:
        return HypothesisReport(tables=[], verified=False)

    query_tool = tools.get("sql_db_query")
    if not query_tool:
        return HypothesisReport(
            tables=[],
            warnings=["SQL 查询工具不可用，跳过假设验证"],
            verified=False,
        )

    governor = get_concurrency_governor()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def probe_table(table: str) -> TableProfile:
        async with semaphore:
            async with governor.acquire("sql"):
                return await _probe_single_table(
                    table,
                    candidate_columns.get(table, []),
                    query_tool,
                    schema_name,
                )

    tasks = [probe_table(t) for t in candidate_tables]
    profiles = list(await asyncio.gather(*tasks, return_exceptions=False))

    warnings: list[str] = []
    for tp in profiles:
        if isinstance(tp, TableProfile):
            if not tp.exists:
                warnings.append(f"表 {tp.table} 不存在，SQL 生成需避免引用")
            for cp in tp.columns:
                if not cp.exists:
                    warnings.append(f"列 {tp.table}.{cp.column} 不存在")
        else:
            warnings.append(f"验证异常: {tp}")

    valid_profiles = [p for p in profiles if isinstance(p, TableProfile)]

    return HypothesisReport(
        tables=valid_profiles,
        warnings=warnings,
        verified=True,
    )


async def _probe_single_table(
    table: str,
    columns: list[str],
    query_tool: BaseTool,
    schema_name: str,
) -> TableProfile:
    """验证单张表及其列。"""
    profile = TableProfile(table=table)

    try:
        count_sql = f"SELECT COUNT(*) AS cnt FROM {schema_name}.{table} LIMIT 1"
        result = str(await query_tool.ainvoke({"query": count_sql}))

        if result.startswith("查询验证失败") or result.startswith("查询执行失败"):
            profile.exists = False
            profile.error = result
            return profile

        profile.exists = True
        try:
            import re
            match = re.search(r"(\d+)", result)
            if match:
                profile.row_count = int(match.group(1))
        except (ValueError, AttributeError):
            pass

    except Exception as e:
        profile.error = str(e)
        return profile

    if not columns:
        return profile

    column_tasks = [
        _probe_column(table, col, query_tool, schema_name)
        for col in columns
    ]
    profile.columns = list(await asyncio.gather(*column_tasks))

    return profile


async def _probe_column(
    table: str,
    column: str,
    query_tool: BaseTool,
    schema_name: str,
) -> ColumnProfile:
    """验证单列并采样 distinct 值。"""
    cp = ColumnProfile(table=table, column=column)

    try:
        distinct_sql = (
            f"SELECT DISTINCT {column}::text AS val "
            f"FROM {schema_name}.{table} "
            f"WHERE {column} IS NOT NULL "
            f"LIMIT {MAX_DISTINCT_SAMPLES}"
        )
        result = str(await query_tool.ainvoke({"query": distinct_sql}))

        if result.startswith("查询验证失败") or result.startswith("查询执行失败"):
            cp.exists = False
            cp.error = result
            return cp

        cp.exists = True

        import re
        vals = re.findall(r"'([^']*)'|(\d[\d.]*)", result)
        cp.distinct_values = [v[0] or v[1] for v in vals][:MAX_DISTINCT_SAMPLES]
        cp.sample_count = len(cp.distinct_values)

    except Exception as e:
        cp.error = str(e)

    return cp


def extract_candidates_from_rag(
    table_names: list[str],
    usage_hints: str,
) -> tuple[list[str], dict[str, list[str]]]:
    """从 RAG 结果中提取候选表和列。

    Returns:
        (candidate_tables, candidate_columns)
    """
    import re
    candidate_tables = list(table_names) if table_names else []

    candidate_columns: dict[str, list[str]] = {}
    for table in candidate_tables:
        cols_in_hints: list[str] = []
        for match in re.finditer(rf"{table}\.(\w+)", usage_hints):
            col = match.group(1)
            if col not in cols_in_hints:
                cols_in_hints.append(col)
        if cols_in_hints:
            candidate_columns[table] = cols_in_hints

    return candidate_tables, candidate_columns
