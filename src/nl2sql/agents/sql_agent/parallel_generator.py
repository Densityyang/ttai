"""多策略并行 SQL 生成 + 锦标赛选优 -- Agentar-Scale-SQL 风格。

对复杂查询启用多策略并行生成：
- 策略 A：直接基于 RAG 证据生成
- 策略 B：基于数据画像 + schema 子集生成
- 策略 C：基于经验记忆中相似查询的模式迁移生成

全部执行后通过执行一致性投票选优（多数一致结果胜出）。
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field

from langchain_core.messages import AnyMessage, SystemMessage
from langchain_core.tools import BaseTool

from src.nl2sql.agents.sql_agent.hypothesis_verifier import HypothesisReport
from src.nl2sql.agents.sql_agent.state import ExplorationResult
from src.nl2sql.infra.governance.semaphore import get_concurrency_governor
from src.nl2sql.infra.llm.gateway import get_legacy_model

logger = logging.getLogger(__name__)

MAX_PARALLEL_STRATEGIES = 3


@dataclass
class SQLCandidate:
    """单个策略生成的 SQL 候选。"""

    strategy: str
    sql: str
    execution_result: str | None = None
    execution_success: bool = False
    result_hash: str = ""
    error: str = ""


@dataclass
class TournamentResult:
    """锦标赛选优结果。"""

    winner: SQLCandidate | None = None
    candidates: list[SQLCandidate] = field(default_factory=list)
    vote_counts: dict[str, int] = field(default_factory=dict)
    consensus: bool = False
    reason: str = ""


# ── 策略提示词 ──────────────────────────────────────────────────────────────────

_STRATEGY_A_PROMPT = """\
你是 PostgreSQL SQL 专家。基于 RAG 检索到的业务参考信息生成 SQL。

{rag_context}

{base_prompt}"""

_STRATEGY_B_PROMPT = """\
你是 PostgreSQL SQL 专家。基于数据画像验证结果生成 SQL，确保引用的表和列已验证存在。

{hypothesis_context}

{base_prompt}"""

_STRATEGY_C_PROMPT = """\
你是 PostgreSQL SQL 专家。基于历史上类似问题的成功 SQL 模式进行迁移生成。

以下是类似问题的成功案例:
{experience_context}

请参考上述模式，为当前问题生成 SQL。
{base_prompt}"""


async def parallel_generate_and_select(
    messages: list[AnyMessage],
    tools: dict[str, BaseTool],
    rag_info: ExplorationResult | None = None,
    hypothesis_report: HypothesisReport | None = None,
    experience_context: str = "",
    prefetched_schema: str | None = None,
) -> TournamentResult:
    """并行生成多候选 SQL 并通过锦标赛选优。

    Args:
        messages: 用户消息
        tools: SQL 工具集
        rag_info: RAG 检索结果
        hypothesis_report: 假设验证报告
        experience_context: 经验记忆上下文
        prefetched_schema: 预取的 schema 信息

    Returns:
        TournamentResult
    """
    strategies = _select_strategies(rag_info, hypothesis_report, experience_context)

    governor = get_concurrency_governor()
    semaphore = asyncio.Semaphore(MAX_PARALLEL_STRATEGIES)

    async def gen_one(strategy: str, prompt: str) -> SQLCandidate:
        async with semaphore:
            async with governor.acquire("sql"):
                return await _generate_single(strategy, prompt)

    tasks = [gen_one(s, p) for s, p in strategies]
    candidates = list(await asyncio.gather(*tasks))

    executable = [c for c in candidates if c.sql]
    if not executable:
        return TournamentResult(
            candidates=candidates,
            reason="所有策略均未能生成有效 SQL",
        )

    query_tool = tools.get("sql_db_query")
    if query_tool:
        await _execute_candidates(executable, query_tool)

    return _tournament_select(executable)


def _build_base_prompt(
    messages: list[AnyMessage],
    prefetched_schema: str | None,
) -> str:
    """构建基础 SQL 生成提示。"""
    question_parts = []
    for msg in messages:
        if msg.type == "human":
            question_parts.append(str(msg.content))

    question = question_parts[-1] if question_parts else "未知问题"
    schema_section = f"\n可用表结构:\n{prefetched_schema}" if prefetched_schema else ""

    return (
        f"用户问题: {question}\n"
        f"{schema_section}\n\n"
        "规则:\n"
        "1. 仅生成 SELECT 语句\n"
        "2. 不要捏造列名或表名\n"
        "3. 日期字符串格式需符合要求，数值计算注意除零保护\n"
        "4. 只返回一条完整的 SQL 语句"
    )


def _select_strategies(
    rag_info: ExplorationResult | None,
    hypothesis_report: HypothesisReport | None,
    experience_context: str,
) -> list[tuple[str, str]]:
    """根据可用上下文选择要启用的策略。"""
    strategies: list[tuple[str, str]] = []

    rag_ctx = ""
    if rag_info and rag_info.has_useful_info:
        rag_ctx = (
            f"相关表: {', '.join(rag_info.table_names)}\n"
            f"业务参考: {rag_info.usage_hints}"
        )
    strategies.append(("strategy_a_rag", _STRATEGY_A_PROMPT.format(
        rag_context=rag_ctx or "（无 RAG 参考信息）",
        base_prompt="{base_prompt}",
    )))

    hypo_ctx = ""
    if hypothesis_report and hypothesis_report.verified:
        hypo_ctx = hypothesis_report.to_context_str()
    strategies.append(("strategy_b_hypothesis", _STRATEGY_B_PROMPT.format(
        hypothesis_context=hypo_ctx or "（无数据画像信息）",
        base_prompt="{base_prompt}",
    )))

    if experience_context:
        strategies.append(("strategy_c_experience", _STRATEGY_C_PROMPT.format(
            experience_context=experience_context,
            base_prompt="{base_prompt}",
        )))

    return strategies


async def _generate_single(strategy: str, prompt_template: str) -> SQLCandidate:
    """单策略 SQL 生成。"""
    candidate = SQLCandidate(strategy=strategy, sql="")

    try:
        llm = get_legacy_model()
        response = await llm.ainvoke([SystemMessage(content=prompt_template)])
        sql = _extract_sql(str(response.content))
        candidate.sql = sql or ""
    except Exception as e:
        candidate.error = str(e)
        logger.warning("策略 %s 生成失败: %s", strategy, e)

    return candidate


async def _execute_candidates(
    candidates: list[SQLCandidate],
    query_tool: BaseTool,
) -> None:
    """并行执行所有候选 SQL。"""
    governor = get_concurrency_governor()

    async def exec_one(c: SQLCandidate) -> None:
        async with governor.acquire("sql"):
            try:
                result = str(await query_tool.ainvoke({"query": c.sql}))
                if result.startswith("查询验证失败") or result.startswith("查询执行失败"):
                    c.execution_success = False
                    c.error = result
                else:
                    c.execution_success = True
                    c.execution_result = result
                    c.result_hash = _hash_result(result)
            except Exception as e:
                c.execution_success = False
                c.error = str(e)

    await asyncio.gather(*[exec_one(c) for c in candidates])


def _tournament_select(candidates: list[SQLCandidate]) -> TournamentResult:
    """执行一致性投票选优。"""
    successful = [c for c in candidates if c.execution_success]

    if not successful:
        best_effort = next((c for c in candidates if c.sql), None)
        return TournamentResult(
            winner=best_effort,
            candidates=candidates,
            reason="所有候选 SQL 执行均失败，选择第一个生成结果",
        )

    if len(successful) == 1:
        return TournamentResult(
            winner=successful[0],
            candidates=candidates,
            vote_counts={successful[0].strategy: 1},
            consensus=True,
            reason=f"仅策略 {successful[0].strategy} 执行成功",
        )

    hash_groups: dict[str, list[SQLCandidate]] = {}
    for c in successful:
        hash_groups.setdefault(c.result_hash, []).append(c)

    vote_counts = {
        group[0].strategy: len(group)
        for group in hash_groups.values()
    }

    best_group = max(hash_groups.values(), key=len)
    consensus = len(best_group) > len(successful) / 2

    winner = best_group[0]

    return TournamentResult(
        winner=winner,
        candidates=candidates,
        vote_counts=vote_counts,
        consensus=consensus,
        reason=(
            f"多数一致 ({len(best_group)}/{len(successful)})"
            if consensus
            else f"无明确共识，选择最大投票组 ({len(best_group)}/{len(successful)})"
        ),
    )


def _hash_result(result: str) -> str:
    """对执行结果计算轻量级哈希（用于一致性比较）。"""
    normalized = result.strip().lower()
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def _extract_sql(text: str) -> str | None:
    """从 LLM 输出中提取 SQL 语句。"""
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
    return content if content else None
