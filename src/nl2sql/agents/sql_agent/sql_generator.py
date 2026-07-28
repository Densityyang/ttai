"""SQL Generator -- 显式 generate-execute-diagnose-repair 闭环。

替代原有的 create_agent 隐式 tool loop，提供可控的修复环路和降级策略。

Phase 2 升级：
- 假设验证（APEX-SQL 数据画像）
- 多策略并行生成 + 锦标赛选优（Agentar-Scale-SQL）
- 经验驱动自修正（Memo-SQL / ReViSQL）
"""

import logging
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import AnyMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.llm.gateway import get_legacy_model
from src.nl2sql.infra.store.graph_rag import expand_with_graph_rag

from .experience_store import get_experience_store
from .hypothesis_verifier import (
    HypothesisReport,
    extract_candidates_from_rag,
    verify_hypotheses,
)
from .parallel_generator import TournamentResult, parallel_generate_and_select
from .state import ExplorationResult

logger = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────


class SqlGeneratorState(TypedDict):
    """SQL Generator 子图状态。"""

    messages: Annotated[list[AnyMessage], add_messages]
    rag_info: NotRequired[ExplorationResult | None]
    prefetched_schema: NotRequired[str | None]
    generated_sql: str | None
    query_result: str | None
    last_error: str | None
    repair_count: int
    table_names_str: str
    # Phase 2 新增
    hypothesis_report: NotRequired[HypothesisReport | None]
    tournament_result: NotRequired[TournamentResult | None]
    use_parallel: NotRequired[bool]


# ── Prompts ───────────────────────────────────────────────────────────────────


def _build_system_prompt(
    rag_info: ExplorationResult | None,
    prefetched_schema: str | None,
    graph_hints: str | None,
) -> str:
    base = (
        "你是一个顶尖的 PostgreSQL 数据库架构师和 SQL 编写专家。\n"
        "你的任务是根据用户的需求，理解已有的表结构，生成准确、无 BUG 的 SQL 语句。\n"
        "生成 SQL 时要注意：\n"
        "1. 仅生成 SELECT 语句。\n"
        "2. 不要捏造列名或表名，必须确认后再查。\n"
        "3. 日期字符串格式需符合要求，数值计算注意除零保护。\n"
        "4. 只返回一条完整的 SQL 语句，不要包含解释。\n\n"
    )

    if rag_info and rag_info.has_useful_info and (rag_info.table_names or rag_info.usage_hints):
        schema_section = (
            f"【目标表结构（已预取）】：\n{prefetched_schema}\n\n"
            if prefetched_schema
            else (
                "【确切目标表名】：\n"
                f"{', '.join(rag_info.table_names) if rag_info.table_names else '无明确表名'}\n\n"
            )
        )
        graph_section = f"【GraphRAG 关联信息】：\n{graph_hints}\n\n" if graph_hints else ""
        return base + (
            schema_section
            + graph_section
            + "【业务用法及计算口诀参考】：\n"
            f"{rag_info.usage_hints}\n\n"
            "请结合上述参考资料，直接写出正确的 SQL 语句。"
        )

    graph_section = f"【GraphRAG 关联信息】：\n{graph_hints}\n\n" if graph_hints else ""
    return base + (
        graph_section
        + "【前置情报】：未找到特别针对该问题的业务名词解释或强相关参考。\n\n"
        "请先分析用户问题，然后生成 SQL。如果不确定表名或字段，请说明。"
    )


_DIAGNOSE_PROMPT = (
    "你是一个 SQL 调试专家。\n"
    "以下 SQL 执行失败，请分析错误原因并给出修复后的完整 SQL。\n"
    "只返回修复后的 SQL，不要包含解释。\n\n"
    "【原始 SQL】：\n{sql}\n\n"
    "【错误信息】：\n{error}\n\n"
    "【可用表结构】：\n{schema}\n"
)


# ── 节点实现 ───────────────────────────────────────────────────────────────────


def _create_hypothesis_node(tools: list[BaseTool]) -> Any:
    """创建假设验证节点（APEX-SQL 数据画像）。"""

    db_tools_by_name = {t.name: t for t in tools}

    async def hypothesis_node(state: SqlGeneratorState) -> dict[str, Any]:
        rag_info: ExplorationResult | None = state.get("rag_info")
        if not rag_info or not rag_info.table_names:
            return {"hypothesis_report": None}

        tables, columns = extract_candidates_from_rag(
            rag_info.table_names, rag_info.usage_hints
        )

        config = get_agent_config()
        report = await verify_hypotheses(
            candidate_tables=tables,
            candidate_columns=columns,
            tools=db_tools_by_name,
            schema_name=config.nl2sql_db_schema,
        )
        logger.info(
            "假设验证完成: %d 表, %d 警告",
            len(report.tables),
            len(report.warnings),
        )
        return {"hypothesis_report": report}

    return hypothesis_node


def _create_generate_node(tools: list[BaseTool]) -> Any:
    """创建 SQL 生成节点（支持并行模式和经验注入）。"""

    db_tools_by_name = {t.name: t for t in tools}

    async def generate_node(state: SqlGeneratorState) -> dict[str, Any]:
        rag_info: ExplorationResult | None = state.get("rag_info")
        prefetched_schema: str | None = state.get("prefetched_schema")
        hypothesis_report: HypothesisReport | None = state.get("hypothesis_report")
        use_parallel: bool = state.get("use_parallel", False)

        graph_hints: str | None = None
        if rag_info and rag_info.table_names:
            expansion = expand_with_graph_rag(rag_info.table_names)
            if expansion.get("join_hints") or expansion.get("expanded_tables"):
                parts = []
                if expansion["expanded_tables"]:
                    parts.append(f"关联表: {', '.join(expansion['expanded_tables'])}")
                if expansion["join_hints"]:
                    parts.append("Join 提示:\n" + "\n".join(expansion["join_hints"]))
                graph_hints = "\n".join(parts)

        if not prefetched_schema:
            list_tool = db_tools_by_name.get("sql_db_list_tables")
            schema_tool = db_tools_by_name.get("sql_db_schema")
            if list_tool:
                tables_str = await list_tool.ainvoke({})
                if schema_tool and tables_str and "没有表" not in tables_str:
                    prefetched_schema = await schema_tool.ainvoke({"table_names": tables_str})

        # 经验记忆注入
        store = get_experience_store()
        question = ""
        for msg in reversed(state["messages"]):
            if msg.type == "human":
                question = str(msg.content)
                break
        experience_ctx = store.format_experience_context(
            question,
            rag_info.table_names if rag_info else None,
        )

        if use_parallel:
            # 多策略并行生成 + 锦标赛选优
            tournament = await parallel_generate_and_select(
                messages=state["messages"],
                tools=db_tools_by_name,
                rag_info=rag_info,
                hypothesis_report=hypothesis_report,
                experience_context=experience_ctx,
                prefetched_schema=prefetched_schema,
            )
            logger.info(
                "锦标赛选优: winner=%s, consensus=%s, reason=%s",
                tournament.winner.strategy if tournament.winner else "none",
                tournament.consensus,
                tournament.reason,
            )
            sql = tournament.winner.sql if tournament.winner else None
            result_dict: dict[str, Any] = {
                "generated_sql": sql,
                "prefetched_schema": prefetched_schema,
                "tournament_result": tournament,
            }
            if tournament.winner and tournament.winner.execution_success:
                result_dict["query_result"] = tournament.winner.execution_result
                result_dict["last_error"] = None
            return result_dict

        # 单策略生成（增强版：注入假设验证 + 经验记忆）
        hypo_section = ""
        if hypothesis_report and hypothesis_report.verified:
            hypo_section = "\n" + hypothesis_report.to_context_str() + "\n"

        exp_section = ""
        if experience_ctx:
            exp_section = "\n" + experience_ctx + "\n"

        system_prompt = _build_system_prompt(rag_info, prefetched_schema, graph_hints)
        if hypo_section:
            system_prompt += hypo_section
        if exp_section:
            system_prompt += exp_section

        llm = get_legacy_model()
        question_msgs = [msg for msg in state["messages"] if msg.type in ("human", "system")]
        all_msgs = [SystemMessage(content=system_prompt)] + question_msgs

        response = await llm.ainvoke(all_msgs)
        sql = _extract_sql(str(response.content))

        return {
            "generated_sql": sql,
            "prefetched_schema": prefetched_schema,
            "messages": [response],
        }

    return generate_node


def _create_execute_node(tools: list[BaseTool]) -> Any:
    """创建 SQL 执行节点。"""

    db_tools_by_name = {t.name: t for t in tools}

    async def execute_node(state: SqlGeneratorState) -> dict[str, Any]:
        sql = state.get("generated_sql")
        if not sql:
            return {"last_error": "未生成有效的 SQL 语句", "query_result": None}

        query_tool = db_tools_by_name.get("sql_db_query")
        if not query_tool:
            return {"last_error": "SQL 执行工具不可用", "query_result": None}

        try:
            result = await query_tool.ainvoke({"query": sql})
            result_str = str(result)

            if result_str.startswith("查询验证失败") or result_str.startswith("查询执行失败"):
                return {"last_error": result_str, "query_result": None}

            return {"query_result": result_str, "last_error": None}
        except Exception as e:
            return {"last_error": str(e), "query_result": None}

    return execute_node


async def diagnose_and_repair_node(state: SqlGeneratorState) -> dict[str, Any]:
    """分析 SQL 错误并生成修复后的 SQL（经验驱动 + LLM 诊断）。"""
    sql = state.get("generated_sql", "")
    error = state.get("last_error") or ""
    schema = state.get("prefetched_schema", "无可用表结构信息")

    store = get_experience_store()
    repair_ctx = store.search_repair_patterns(error)

    if repair_ctx:
        best_match = repair_ctx[0]
        logger.info(
            "经验驱动修复命中: error_type=%s, hit_count=%d",
            best_match.error_type,
            best_match.hit_count,
        )
        repair_hint = (
            f"\n\n【历史修复经验参考】\n"
            f"类似错误 ({best_match.error_type}): {best_match.error_message[:100]}\n"
            f"修复方式: {best_match.repair_explanation or '直接修复'}\n"
            f"修复后 SQL 参考: {best_match.repaired_sql[:300]}"
        )
    else:
        repair_hint = ""

    prompt = _DIAGNOSE_PROMPT.format(sql=sql, error=error, schema=schema) + repair_hint
    llm = get_legacy_model()

    response = await llm.ainvoke([SystemMessage(content=prompt)])
    repaired_sql = _extract_sql(str(response.content))

    return {
        "generated_sql": repaired_sql,
        "repair_count": state.get("repair_count", 0) + 1,
    }


async def format_node(state: SqlGeneratorState) -> dict[str, Any]:
    """格式化成功的查询结果为用户友好的回答，并记录经验。"""
    llm = get_legacy_model()
    question_msgs = [msg for msg in state["messages"] if msg.type == "human"]
    question = str(question_msgs[-1].content) if question_msgs else ""

    result = state.get("query_result", "")
    sql = state.get("generated_sql", "")

    prompt = (
        "你是一个数据分析师。请根据用户问题和查询结果，给出简洁的业务回答。\n"
        "优先给出结论，再补充关键数据。不展示 SQL。字段名转换为业务可读表达。\n\n"
        f"用户问题：{question}\n\n"
        f"查询结果：{result}\n"
    )

    response = await llm.ainvoke([SystemMessage(content=prompt)])

    # 记录成功经验
    rag_info_value = state.get("rag_info")
    rag_info = rag_info_value if isinstance(rag_info_value, ExplorationResult) else None
    store = get_experience_store()
    if sql and question:
        store.record_success(
            question=question,
            table_names=rag_info.table_names if rag_info else [],
            schema_snippet=str(state.get("prefetched_schema") or "")[:500],
            sql=sql,
            result_summary=str(result)[:200],
        )

    # 如果是经过修复后成功的，记录修复对
    if state.get("repair_count", 0) > 0 and state.get("last_error"):
        store.record_repair(
            error_sql=sql or "",
            error_message=state.get("last_error") or "",
            repaired_sql=sql or "",
        )

    from langchain_core.messages import AIMessage
    return {"messages": [AIMessage(content=str(response.content))]}


async def fallback_node(state: SqlGeneratorState) -> dict[str, Any]:
    """修复次数耗尽，返回降级响应。"""
    error = state.get("last_error") or "未知错误"
    repair_count = state.get("repair_count", 0)

    from langchain_core.messages import AIMessage
    fallback_msg = (
        f"抱歉，经过 {repair_count} 次尝试仍未能成功执行查询。\n"
        f"最后一次错误：{error}\n\n"
        "建议：\n"
        "1. 尝试简化您的问题\n"
        "2. 明确指定您想查询的具体指标或表\n"
        "3. 检查时间范围或筛选条件是否合理"
    )
    return {"messages": [AIMessage(content=fallback_msg)]}


# ── 路由 ──────────────────────────────────────────────────────────────────────


def after_execute_router(state: SqlGeneratorState) -> Literal["format", "diagnose_repair", "fallback"]:
    """执行后路由：成功 -> format，失败且可重试 -> diagnose，超限 -> fallback。"""
    if state.get("query_result") is not None and state.get("last_error") is None:
        return "format"

    config = get_agent_config()
    if state.get("repair_count", 0) < config.sql_max_repair_rounds:
        return "diagnose_repair"

    return "fallback"


# ── 工具函数 ──────────────────────────────────────────────────────────────────


def _extract_sql(text: str) -> str | None:
    """从 LLM 输出中提取 SQL 语句。"""
    content = text.strip()
    if "```sql" in content:
        start = content.index("```sql") + 6
        end = content.index("```", start) if "```" in content[start:] else len(content)
        content = content[start:end].strip()
    elif "```" in content:
        start = content.index("```") + 3
        end = content.index("```", start) if "```" in content[start:] else len(content)
        content = content[start:end].strip()

    upper = content.upper().lstrip()
    if upper.startswith("SELECT") or upper.startswith("WITH"):
        return content

    for line in content.split("\n"):
        stripped = line.strip().upper()
        if stripped.startswith("SELECT") or stripped.startswith("WITH"):
            return line.strip()

    return content if content else None


# ── 图构建 ─────────────────────────────────────────────────────────────────────


def _after_hypothesis_router(state: SqlGeneratorState) -> Literal["generate_parallel", "generate"]:
    """假设验证后路由：根据查询复杂度决定是否并行。"""
    rag_info: ExplorationResult | None = state.get("rag_info")
    hypothesis: HypothesisReport | None = state.get("hypothesis_report")

    if not rag_info:
        return "generate"

    table_count = len(rag_info.table_names) if rag_info.table_names else 0
    has_warnings = bool(hypothesis and hypothesis.warnings)
    hints_long = len(rag_info.usage_hints) > 200 if rag_info.usage_hints else False

    if table_count >= 2 or has_warnings or hints_long:
        return "generate_parallel"
    return "generate"


def _create_parallel_generate_node(tools: list[BaseTool]) -> Any:
    """创建并行生成节点的包装。"""
    inner_node = _create_generate_node(tools)

    async def parallel_node(state: SqlGeneratorState) -> dict[str, Any]:
        patched = {**state, "use_parallel": True}
        return await inner_node(patched)

    return parallel_node


def _after_parallel_generate_router(
    state: SqlGeneratorState,
) -> Literal["format", "execute", "diagnose_repair", "fallback"]:
    """并行生成后路由：已有执行结果 -> format，否则 -> execute。"""
    tournament: TournamentResult | None = state.get("tournament_result")
    if tournament and tournament.winner and tournament.winner.execution_success:
        return "format"
    if state.get("generated_sql"):
        return "execute"
    return "fallback"


def create_sql_generator(tools: list[Any], enable_hypothesis: bool = True) -> Any:
    """创建 SQL Generator 子图（Phase 2 增强版）。

    Args:
        tools: SQL 工具列表
        enable_hypothesis: 是否启用假设验证（默认启用）
    """
    builder: StateGraph = StateGraph(SqlGeneratorState)

    builder.add_node("generate", _create_generate_node(tools))
    builder.add_node("execute", _create_execute_node(tools))
    builder.add_node("diagnose_repair", diagnose_and_repair_node)
    builder.add_node("format", format_node)
    builder.add_node("fallback", fallback_node)

    if enable_hypothesis:
        builder.add_node("hypothesis", _create_hypothesis_node(tools))
        builder.add_node("generate_parallel", _create_parallel_generate_node(tools))

        builder.add_edge(START, "hypothesis")
        builder.add_conditional_edges(
            "hypothesis",
            _after_hypothesis_router,
            {"generate_parallel": "generate_parallel", "generate": "generate"},
        )
        builder.add_conditional_edges(
            "generate_parallel",
            _after_parallel_generate_router,
            {
                "format": "format",
                "execute": "execute",
                "diagnose_repair": "diagnose_repair",
                "fallback": "fallback",
            },
        )
    else:
        builder.add_edge(START, "generate")

    builder.add_edge("generate", "execute")
    builder.add_conditional_edges(
        "execute",
        after_execute_router,
        {
            "format": "format",
            "diagnose_repair": "diagnose_repair",
            "fallback": "fallback",
        },
    )
    builder.add_edge("diagnose_repair", "execute")
    builder.add_edge("format", END)
    builder.add_edge("fallback", END)

    return builder.compile(name="sql_generator")
