"""动态指标计算 Agent 图 -- CodeAct 核心链路。

流程：plan -> data_fetch(SQL取数循环) -> code_gen -> code_exec -> validate -> format
支持代码修复环路和降级策略。
"""

import asyncio
import logging
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from src.nl2sql.agents.dynamic_calc.code_executor import SandboxExecutor
from src.nl2sql.agents.dynamic_calc.planner import generate_calc_plan
from src.nl2sql.agents.dynamic_calc.prompts import (
    CODE_GENERATOR_SYSTEM_PROMPT,
    CODE_REPAIR_SYSTEM_PROMPT,
)
from src.nl2sql.agents.dynamic_calc.schemas import (
    DynamicCalcPlan,
    DynamicCalcResult,
    SandboxResult,
)
from src.nl2sql.agents.dynamic_calc.trusted_templates import (
    TrustedTemplateError,
    trusted_template_registry,
)
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.llm.gateway import get_legacy_model
from src.nl2sql.infra.store.database import get_nl2sql_db_manager
from src.nl2sql.tools.async_sql_tools import create_async_sql_tools

logger = logging.getLogger(__name__)

_default_dynamic_calc_graph: Any | None = None
_default_dynamic_calc_graph_lock = asyncio.Lock()


# ── State ─────────────────────────────────────────────────────────────────────


class DynamicCalcState(TypedDict):
    """动态计算 Agent 状态。"""

    messages: Annotated[list[AnyMessage], add_messages]
    plan: NotRequired[DynamicCalcPlan | None]
    data_frames_info: list[dict[str, Any]]
    generated_code: str | None
    sandbox_result: NotRequired[SandboxResult | None]
    calc_result: NotRequired[DynamicCalcResult | None]
    code_repair_count: int
    last_code_error: str | None


# ── 节点实现 ───────────────────────────────────────────────────────────────────


async def plan_node(state: DynamicCalcState) -> dict[str, Any]:
    """生成动态计算计划。"""
    question = ""
    for msg in reversed(state["messages"]):
        if msg.type == "human":
            question = str(msg.content)
            break

    plan = await generate_calc_plan(question)
    return {"plan": plan}


def _create_data_fetch_node(tools: list[BaseTool]) -> Any:
    """创建数据取数节点。"""

    db_tools_by_name = {t.name: t for t in tools}

    async def data_fetch_node(state: DynamicCalcState) -> dict[str, Any]:
        plan = state.get("plan")
        if not plan or not plan.data_steps:
            return {"data_frames_info": []}

        question = ""
        for msg in reversed(state["messages"]):
            if msg.type == "human":
                question = str(msg.content)
                break

        llm = get_legacy_model()
        query_tool = db_tools_by_name.get("sql_db_query")
        list_tool = db_tools_by_name.get("sql_db_list_tables")
        schema_tool = db_tools_by_name.get("sql_db_schema")

        tables_str = ""
        schema_str = ""
        if list_tool:
            tables_str = str(await list_tool.ainvoke({}))
        if schema_tool and tables_str:
            schema_str = str(await schema_tool.ainvoke({"table_names": tables_str}))

        data_frames_info: list[dict[str, Any]] = []

        for step in plan.data_steps:
            sql_prompt = (
                f"根据以下需求生成一条 SELECT SQL 查询。\n\n"
                f"用户原始问题：{question}\n"
                f"取数步骤描述：{step.description}\n"
                f"期望输出：{step.expected_output}\n\n"
                f"可用表结构：\n{schema_str}\n\n"
                "只返回 SQL，不要解释。"
            )

            try:
                response = await llm.ainvoke([SystemMessage(content=sql_prompt)])
                sql = _extract_sql_from_response(str(response.content))

                if sql and query_tool:
                    result = str(await query_tool.ainvoke({"query": sql}))
                    if not result.startswith("查询验证失败") and not result.startswith("查询执行失败"):
                        data_frames_info.append({
                            "step_id": step.step_id,
                            "description": step.description,
                            "sql": sql,
                            "data": result,
                        })
                    else:
                        data_frames_info.append({
                            "step_id": step.step_id,
                            "description": step.description,
                            "sql": sql,
                            "error": result,
                        })
            except Exception as e:
                logger.warning("取数步骤 %d 失败: %s", step.step_id, e)
                data_frames_info.append({
                    "step_id": step.step_id,
                    "description": step.description,
                    "error": str(e),
                })

        return {"data_frames_info": data_frames_info}

    return data_fetch_node


async def code_gen_node(state: DynamicCalcState) -> dict[str, Any]:
    """生成计算代码。"""
    plan = state.get("plan")
    data_frames_info = state.get("data_frames_info", [])

    question = ""
    for msg in reversed(state["messages"]):
        if msg.type == "human":
            question = str(msg.content)
            break

    data_context_desc = []
    for i, df_info in enumerate(data_frames_info):
        if "error" in df_info:
            data_context_desc.append(f"df_{i}: 取数失败 - {df_info['error']}")
        else:
            data_context_desc.append(
                f"df_{i}: {df_info['description']}\n"
                f"  SQL: {df_info.get('sql', 'N/A')}\n"
                f"  数据预览: {str(df_info.get('data', ''))[:500]}"
            )

    calc_desc = ""
    if plan and plan.calc_steps:
        calc_desc = "\n".join(
            f"步骤 {s.step_id}: {s.description} -> {s.expected_output}"
            for s in plan.calc_steps
        )

    prompt = (
        f"用户需求：{question}\n\n"
        f"计算步骤：\n{calc_desc}\n\n"
        f"可用数据：\n" + "\n".join(data_context_desc) + "\n\n"
        "注意：数据变量 df_0, df_1, ... 已经是 pandas DataFrame。\n"
        "请生成完整的计算代码。"
    )

    llm = get_legacy_model()
    response = await llm.ainvoke([
        SystemMessage(content=CODE_GENERATOR_SYSTEM_PROMPT),
        SystemMessage(content=prompt),
    ])

    code = _extract_code_from_response(str(response.content))
    return {"generated_code": code}


async def code_exec_node(state: DynamicCalcState) -> dict[str, Any]:
    """Execute only an approved template or explicitly unsafe development code."""
    config = get_agent_config()
    if not config.enable_dynamic_calc or config.codeact_mode == "disabled":
        return {
            "sandbox_result": SandboxResult(
                success=False,
                error="dynamic calculation is disabled by runtime policy",
            ),
        }
    if config.codeact_mode == "trusted-template":
        plan = state.get("plan")
        if not plan or not plan.trusted_template_id:
            return {
                "sandbox_result": SandboxResult(
                    success=False,
                    error="trusted-template mode requires an approved template identifier",
                ),
            }
        try:
            output = trusted_template_registry.execute(
                plan.trusted_template_id,
                plan.trusted_template_inputs,
            )
        except TrustedTemplateError as exc:
            return {"sandbox_result": SandboxResult(success=False, error=str(exc))}
        return {
            "sandbox_result": SandboxResult(
                success=True,
                result=output.model_dump(mode="json"),
                stats={"template_id": plan.trusted_template_id},
            ),
        }
    code = state.get("generated_code")
    if not code:
        return {
            "sandbox_result": SandboxResult(success=False, error="未生成有效代码"),
        }

    data_context: dict[str, Any] = {}
    for i, df_info in enumerate(state.get("data_frames_info", [])):
        if "data" in df_info and "error" not in df_info:
            try:
                import pandas as pd
                data = df_info["data"]
                if isinstance(data, str):
                    import ast as _ast
                    parsed = _ast.literal_eval(data)
                    if isinstance(parsed, list):
                        data_context[f"df_{i}"] = pd.DataFrame(parsed)
                    elif isinstance(parsed, dict):
                        data_context[f"df_{i}"] = pd.DataFrame([parsed])
                    else:
                        data_context[f"df_{i}"] = pd.DataFrame()
                else:
                    data_context[f"df_{i}"] = pd.DataFrame()
            except Exception:
                import pandas as pd
                data_context[f"df_{i}"] = pd.DataFrame()

    try:
        import pandas as pd
        data_context["pd"] = pd
    except ImportError:
        pass

    try:
        import numpy as np
        data_context["np"] = np
    except ImportError:
        pass

    executor = SandboxExecutor()
    result = await executor.execute(code, data_context)
    return {"sandbox_result": result}


async def code_repair_node(state: DynamicCalcState) -> dict[str, Any]:
    """修复执行失败的代码。"""
    code = state.get("generated_code", "")
    sandbox_result = state.get("sandbox_result")
    error = sandbox_result.error if sandbox_result else "未知错误"

    data_desc = ", ".join(
        f"df_{i}" for i, info in enumerate(state.get("data_frames_info", []))
        if "error" not in info
    )

    prompt = CODE_REPAIR_SYSTEM_PROMPT.format(
        code=code,
        error=error,
        data_context=data_desc or "无可用数据",
    )

    llm = get_legacy_model()
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    repaired = _extract_code_from_response(str(response.content))

    return {
        "generated_code": repaired,
        "code_repair_count": state.get("code_repair_count", 0) + 1,
    }


async def format_result_node(state: DynamicCalcState) -> dict[str, Any]:
    """格式化计算结果为用户友好的回答。"""
    sandbox_result = state.get("sandbox_result")

    if sandbox_result and sandbox_result.success:
        calc_result = DynamicCalcResult(
            final_value=sandbox_result.result,
            intermediate_stats=sandbox_result.stats,
            execution_summary=f"计算成功，耗时 {sandbox_result.elapsed_ms:.0f}ms",
            steps_log=[
                {"step": "data_fetch", "frames": len(state.get("data_frames_info", []))},
                {"step": "code_exec", "elapsed_ms": sandbox_result.elapsed_ms},
            ],
        )

        llm = get_legacy_model()
        question = ""
        for msg in reversed(state["messages"]):
            if msg.type == "human":
                question = str(msg.content)
                break

        prompt = (
            f"用户问题：{question}\n"
            f"计算结果：{sandbox_result.result}\n"
            f"中间统计：{sandbox_result.stats}\n\n"
            "请用通俗易懂的语言回答用户问题，先给结论再给细节。"
        )
        response = await llm.ainvoke([SystemMessage(content=prompt)])

        return {
            "calc_result": calc_result,
            "messages": [AIMessage(content=str(response.content))],
        }

    return {
        "messages": [AIMessage(content="动态计算未能产出有效结果。")],
    }


async def fallback_node(state: DynamicCalcState) -> dict[str, Any]:
    """降级响应。"""
    plan = state.get("plan")
    error = state.get("last_code_error") or "计算过程中出现错误"
    repair_count = state.get("code_repair_count", 0)

    msg = (
        f"抱歉，动态计算经过 {repair_count} 次尝试仍未成功。\n"
        f"错误信息：{error}\n\n"
    )
    if plan and plan.fallback_strategy == "report_partial":
        successful_data = [
            info for info in state.get("data_frames_info", [])
            if "error" not in info
        ]
        if successful_data:
            msg += "已成功获取的数据：\n"
            for info in successful_data:
                msg += f"- {info['description']}: {str(info.get('data', ''))[:200]}\n"

    msg += "\n建议：尝试更明确地描述计算逻辑，或将问题拆分为更简单的子问题。"
    return {"messages": [AIMessage(content=msg)]}


# ── 路由 ──────────────────────────────────────────────────────────────────────


def after_plan_router(state: DynamicCalcState) -> Literal["data_fetch", "code_exec", "fallback"]:
    """计划生成后路由。"""
    plan = state.get("plan")
    if get_agent_config().codeact_mode == "trusted-template":
        if plan and plan.trusted_template_id:
            return "code_exec"
        return "fallback"
    if not plan or plan.fallback_strategy == "abort" or not plan.data_steps:
        return "fallback"
    return "data_fetch"


def after_data_fetch_router(state: DynamicCalcState) -> Literal["code_gen", "fallback"]:
    """取数后路由。"""
    data_frames = state.get("data_frames_info", [])
    successful = [info for info in data_frames if "error" not in info]
    if not successful:
        return "fallback"
    return "code_gen"


def after_code_exec_router(state: DynamicCalcState) -> Literal["format_result", "code_repair", "fallback"]:
    """代码执行后路由。"""
    sandbox_result = state.get("sandbox_result")
    if sandbox_result and sandbox_result.success:
        return "format_result"

    config = get_agent_config()
    if config.codeact_mode != "unsafe-dev":
        return "fallback"
    if state.get("code_repair_count", 0) < config.code_max_repair_rounds:
        return "code_repair"

    return "fallback"


# ── 工具函数 ──────────────────────────────────────────────────────────────────


def _extract_sql_from_response(text: str) -> str | None:
    """从 LLM 输出中提取 SQL。"""
    content = text.strip()
    if "```sql" in content:
        start = content.index("```sql") + 6
        end_marker = content.find("```", start)
        end = end_marker if end_marker != -1 else len(content)
        return content[start:end].strip()
    if "```" in content:
        start = content.index("```") + 3
        end_marker = content.find("```", start)
        end = end_marker if end_marker != -1 else len(content)
        return content[start:end].strip()

    upper = content.upper().lstrip()
    if upper.startswith("SELECT") or upper.startswith("WITH"):
        return content
    return None


def _extract_code_from_response(text: str) -> str | None:
    """从 LLM 输出中提取 Python 代码。"""
    content = text.strip()
    if "```python" in content:
        start = content.index("```python") + 9
        end_marker = content.find("```", start)
        end = end_marker if end_marker != -1 else len(content)
        return content[start:end].strip()
    if "```" in content:
        start = content.index("```") + 3
        end_marker = content.find("```", start)
        end = end_marker if end_marker != -1 else len(content)
        return content[start:end].strip()
    return content if content else None


# ── 图构建 ─────────────────────────────────────────────────────────────────────


async def build_dynamic_calc_graph(
    database_url: str | None = None,
) -> Any:
    """构建动态指标计算 Agent 图。"""
    agent_config = get_agent_config()

    db_manager = await get_nl2sql_db_manager(
        database_url=database_url,
        schema=agent_config.nl2sql_db_schema,
    )
    all_tools = create_async_sql_tools(db_manager)

    builder: StateGraph = StateGraph(DynamicCalcState)

    builder.add_node("plan", plan_node)
    builder.add_node("data_fetch", _create_data_fetch_node(all_tools))
    builder.add_node("code_gen", code_gen_node)
    builder.add_node("code_exec", code_exec_node)
    builder.add_node("code_repair", code_repair_node)
    builder.add_node("format_result", format_result_node)
    builder.add_node("fallback", fallback_node)

    builder.add_edge(START, "plan")
    builder.add_conditional_edges(
        "plan", after_plan_router,
        {"data_fetch": "data_fetch", "code_exec": "code_exec", "fallback": "fallback"},
    )
    builder.add_conditional_edges(
        "data_fetch", after_data_fetch_router,
        {"code_gen": "code_gen", "fallback": "fallback"},
    )
    builder.add_edge("code_gen", "code_exec")
    builder.add_conditional_edges(
        "code_exec", after_code_exec_router,
        {"format_result": "format_result", "code_repair": "code_repair", "fallback": "fallback"},
    )
    builder.add_edge("code_repair", "code_exec")
    builder.add_edge("format_result", END)
    builder.add_edge("fallback", END)

    graph = builder.compile(name="dynamic_calc_agent")
    logger.info("动态指标计算 Agent 图构建成功")
    return graph


async def get_default_dynamic_calc_graph() -> Any:
    """获取默认动态计算 Agent 图单例。"""
    global _default_dynamic_calc_graph

    if _default_dynamic_calc_graph is not None:
        return _default_dynamic_calc_graph

    async with _default_dynamic_calc_graph_lock:
        if _default_dynamic_calc_graph is None:
            _default_dynamic_calc_graph = await build_dynamic_calc_graph()

    return _default_dynamic_calc_graph
