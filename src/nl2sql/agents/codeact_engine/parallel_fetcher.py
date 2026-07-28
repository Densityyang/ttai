"""并行取数器 -- 根据 ConfirmedCalcPlan 的取数指令并行执行 SQL。

特性：
- 并行执行取数步骤（受 semaphore 保护）
- 自动生成取数 SQL（数据来源和筛选条件已由用户确认）
- 失败步骤可独立重试，不阻塞其他步骤
- 取数后做数据完整性快检
"""

import asyncio
import logging
from dataclasses import dataclass, field

from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool

from src.nl2sql.agents.codeact_engine.plan_card import DataFetchStep
from src.nl2sql.infra.llm.gateway import get_legacy_model

logger = logging.getLogger(__name__)

MAX_CONCURRENT_FETCHES = 3
MAX_RETRY_PER_STEP = 2

FETCH_SQL_PROMPT = """\
根据以下取数需求生成一条 SELECT SQL:

表名: {table}
需要字段: {fields}
筛选条件: {filters}
描述: {description}

可用表结构:
{schema_info}

规则:
1. 只返回 SQL，不要解释
2. 使用 SELECT 语句
3. 精确使用指定的表名和字段名
4. 加上合理的 LIMIT (最大 50000 行)
5. 如果字段列表为空，使用 SELECT *"""


@dataclass
class FetchResult:
    """单步取数结果。"""

    step_id: int
    description: str
    success: bool
    sql: str = ""
    data: str = ""
    row_count: int = 0
    error: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class FetchReport:
    """并行取数的汇总报告。"""

    results: list[FetchResult]
    total_steps: int
    successful_steps: int
    failed_steps: int
    warnings: list[str] = field(default_factory=list)

    @property
    def all_success(self) -> bool:
        return self.failed_steps == 0

    @property
    def has_data(self) -> bool:
        return self.successful_steps > 0


async def parallel_fetch(
    fetch_steps: tuple[DataFetchStep, ...],
    tools: dict[str, BaseTool],
    schema_info: str = "",
) -> FetchReport:
    """并行执行取数步骤。

    Args:
        fetch_steps: 取数指令序列
        tools: SQL 工具集 (name -> tool)
        schema_info: 可用表结构文本

    Returns:
        FetchReport 汇总报告
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
    results: list[FetchResult] = []

    async def _fetch_one(step: DataFetchStep) -> FetchResult:
        async with semaphore:
            return await _execute_fetch_step(step, tools, schema_info)

    tasks = [_fetch_one(step) for step in fetch_steps]
    results = list(await asyncio.gather(*tasks, return_exceptions=False))

    successful = sum(1 for r in results if r.success)
    failed = len(results) - successful
    warnings: list[str] = []

    for r in results:
        warnings.extend(r.warnings)

    if failed > 0 and successful == 0:
        warnings.append("所有取数步骤均失败，无法继续计算")
    elif failed > 0:
        warnings.append(f"{failed} 个取数步骤失败，将使用部分数据继续计算")

    return FetchReport(
        results=results,
        total_steps=len(fetch_steps),
        successful_steps=successful,
        failed_steps=failed,
        warnings=warnings,
    )


async def _execute_fetch_step(
    step: DataFetchStep,
    tools: dict[str, BaseTool],
    schema_info: str,
) -> FetchResult:
    """执行单个取数步骤，支持重试。"""
    query_tool = tools.get("sql_db_query")
    if not query_tool:
        return FetchResult(
            step_id=step.step_id,
            description=step.description,
            success=False,
            error="SQL 查询工具不可用",
        )

    last_error = ""
    for attempt in range(MAX_RETRY_PER_STEP + 1):
        try:
            sql = await _generate_fetch_sql(step, schema_info)
            if not sql:
                last_error = "无法生成有效 SQL"
                continue

            raw_result = str(await query_tool.ainvoke({"query": sql}))

            if raw_result.startswith("查询验证失败") or raw_result.startswith("查询执行失败"):
                last_error = raw_result
                continue

            row_count = _estimate_row_count(raw_result)
            warnings: list[str] = []

            if row_count == 0:
                warnings.append(f"步骤 {step.step_id} 返回 0 行数据，筛选条件可能过严")

            return FetchResult(
                step_id=step.step_id,
                description=step.description,
                success=True,
                sql=sql,
                data=raw_result,
                row_count=row_count,
                warnings=warnings,
            )

        except Exception as e:
            last_error = str(e)
            logger.warning(
                "取数步骤 %d 第 %d 次尝试失败: %s",
                step.step_id,
                attempt + 1,
                e,
            )

    return FetchResult(
        step_id=step.step_id,
        description=step.description,
        success=False,
        error=f"经 {MAX_RETRY_PER_STEP + 1} 次尝试后仍失败: {last_error}",
    )


async def _generate_fetch_sql(step: DataFetchStep, schema_info: str) -> str | None:
    """基于 DataFetchStep 生成取数 SQL。"""
    fields_str = ", ".join(step.expected_columns) if step.expected_columns else "*"
    filters_desc = "; ".join(
        f"{f.field} {f.operator} {f.value}" for f in step.filters
    ) if step.filters else "无"

    prompt = FETCH_SQL_PROMPT.format(
        table=step.source.table,
        fields=fields_str,
        filters=filters_desc,
        description=step.description,
        schema_info=schema_info or "（无可用 schema 信息）",
    )

    llm = get_legacy_model()
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    return _extract_sql(str(response.content))


def _extract_sql(text: str) -> str | None:
    """从 LLM 输出中提取 SQL。"""
    content = text.strip()
    if "```sql" in content:
        start = content.index("```sql") + 6
        end = content.find("```", start)
        return content[start:end if end != -1 else len(content)].strip()
    if "```" in content:
        start = content.index("```") + 3
        end = content.find("```", start)
        return content[start:end if end != -1 else len(content)].strip()

    upper = content.upper().lstrip()
    if upper.startswith("SELECT") or upper.startswith("WITH"):
        return content
    return None


def _estimate_row_count(raw_result: str) -> int:
    """粗略估计结果行数。"""
    if not raw_result or raw_result.strip() == "[]":
        return 0
    return max(raw_result.count("\n"), raw_result.count("),("), raw_result.count("}, {"))
