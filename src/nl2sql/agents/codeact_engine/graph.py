"""CodeAct Engine LangGraph 图 -- HITL 确认 + 动态指标计算编排。

新架构流程：
  HITL 入口 -> 结构化分解 -> 计划卡片 -> 用户确认 -> 锁定 ->
  计划摄入 -> 并行取数 -> 代码生成 -> 沙箱执行 -> 结果验证 ->
  格式化输出

与旧 dynamic_calc 的关键区别：
1. 不再自行猜测用户意图，由 HITL 层保证输入明确性
2. 沙箱升级为进程隔离
3. 结果对照 ConfirmedCalcPlan 做多维验证
4. 异常情况回退到 HITL 而非自行修复计划
"""

import asyncio
import logging
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from src.nl2sql.agents.codeact_engine.code_generator import generate_code, repair_code
from src.nl2sql.agents.codeact_engine.hitl_protocol import (
    decompose_to_plan_card,
    lock_plan,
    refine_plan_card,
)
from src.nl2sql.agents.codeact_engine.parallel_fetcher import (
    FetchReport,
    parallel_fetch,
)
from src.nl2sql.agents.codeact_engine.plan_card import (
    CalcPlanCard,
    ConfirmedCalcPlan,
)
from src.nl2sql.agents.codeact_engine.plan_ingestion import (
    IngestedPlan,
    PlanIngestionError,
    ingest,
)
from src.nl2sql.agents.codeact_engine.process_sandbox import ProcessSandbox
from src.nl2sql.agents.codeact_engine.prompts import FORMAT_RESULT_PROMPT
from src.nl2sql.agents.codeact_engine.validator import (
    ValidationReport,
    validate_result,
)
from src.nl2sql.agents.dynamic_calc.schemas import SandboxResult
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.llm.gateway import get_legacy_model
from src.nl2sql.infra.store.database import get_nl2sql_db_manager
from src.nl2sql.tools.async_sql_tools import create_async_sql_tools

logger = logging.getLogger(__name__)

_default_codeact_graph: Any | None = None
_default_codeact_graph_lock = asyncio.Lock()


# ── State ─────────────────────────────────────────────────────────────────────


class CodeActState(TypedDict):
    """CodeAct Engine 状态。"""

    messages: Annotated[list[AnyMessage], add_messages]

    # HITL 阶段
    plan_card: NotRequired[CalcPlanCard | None]
    hitl_status: str  # "pending" | "awaiting_confirmation" | "confirmed" | "rejected"
    user_action: NotRequired[str | None]  # "confirm" | "modify" | "restart"
    user_feedback: NotRequired[str | None]

    # 执行阶段
    confirmed_plan: NotRequired[ConfirmedCalcPlan | None]
    ingested_plan: NotRequired[IngestedPlan | None]
    fetch_report: NotRequired[FetchReport | None]
    generated_code: NotRequired[str | None]
    sandbox_result: NotRequired[SandboxResult | None]
    validation_report: NotRequired[ValidationReport | None]
    code_repair_count: int
    hitl_fallback_reason: NotRequired[str | None]

    # 上下文
    schema_context: NotRequired[str]


# ── HITL 节点 ─────────────────────────────────────────────────────────────────


async def decompose_node(state: CodeActState) -> dict[str, Any]:
    """将用户问题分解为结构化 CalcPlanCard。"""
    question = _get_last_human_message(state)
    schema_ctx = state.get("schema_context", "")

    card = await decompose_to_plan_card(question, schema_ctx)

    return {
        "plan_card": card,
        "hitl_status": "awaiting_confirmation",
        "messages": [AIMessage(content=card.to_markdown())],
    }


async def refine_node(state: CodeActState) -> dict[str, Any]:
    """根据用户反馈修正计划卡片。"""
    current_card = state.get("plan_card")
    feedback = state.get("user_feedback", "")

    if not current_card:
        return {"hitl_status": "pending"}

    refined = await refine_plan_card(current_card, feedback or "")
    return {
        "plan_card": refined,
        "hitl_status": "awaiting_confirmation",
        "messages": [AIMessage(content=refined.to_markdown())],
    }


async def lock_node(state: CodeActState) -> dict[str, Any]:
    """锁定计划并生成 ConfirmedCalcPlan。"""
    card = state.get("plan_card")
    if not card:
        return {
            "hitl_status": "rejected",
            "messages": [AIMessage(content="无法锁定空计划，请重新描述需求。")],
        }

    confirmed = lock_plan(card)
    return {
        "confirmed_plan": confirmed,
        "hitl_status": "confirmed",
    }


# ── 执行节点 ──────────────────────────────────────────────────────────────────


async def ingest_node(state: CodeActState) -> dict[str, Any]:
    """摄入并校验确认后的计划。"""
    confirmed = state.get("confirmed_plan")
    if not confirmed:
        return {
            "hitl_fallback_reason": "计划数据丢失",
            "messages": [AIMessage(content="内部错误：确认计划数据丢失。")],
        }

    try:
        plan = ingest(confirmed)
        msgs: dict[str, Any] = {"ingested_plan": plan}
        if plan.warnings:
            msgs["messages"] = [AIMessage(content="⚠ " + "; ".join(plan.warnings))]
        return msgs
    except PlanIngestionError as e:
        logger.warning("计划摄入失败: %s (recoverable=%s)", e.reason, e.recoverable)
        return {
            "hitl_fallback_reason": e.reason,
            "messages": [AIMessage(
                content=f"计划校验发现问题: {e.reason}\n请修改计划后重新确认。"
            )],
        }


def _create_fetch_node(tools: list[BaseTool]) -> Any:
    """创建取数节点（闭包捕获工具集）。"""
    tools_map = {t.name: t for t in tools}

    async def fetch_node(state: CodeActState) -> dict[str, Any]:
        plan = state.get("ingested_plan")
        if not plan or not plan.fetch_steps:
            return {"fetch_report": FetchReport([], 0, 0, 0, ["无取数步骤"])}

        schema_ctx = state.get("schema_context", "")
        report = await parallel_fetch(plan.fetch_steps, tools_map, schema_ctx)

        if not report.has_data:
            return {
                "fetch_report": report,
                "hitl_fallback_reason": "所有取数步骤均失败，可能筛选条件有误",
                "messages": [AIMessage(content="取数失败: " + "; ".join(report.warnings))],
            }

        return {"fetch_report": report}

    return fetch_node


async def code_gen_node(state: CodeActState) -> dict[str, Any]:
    """基于确认计划生成计算代码。"""
    plan = state.get("ingested_plan")
    report = state.get("fetch_report")

    if not plan or not report:
        return {"generated_code": None}

    successful_results = [r for r in report.results if r.success]

    code = await generate_code(
        compute_steps=plan.compute_steps,
        formula=plan.formula,
        fetch_results=successful_results,
        output_type=plan.output_type,
        output_precision=plan.output_precision,
        output_unit=plan.output_unit,
    )
    return {"generated_code": code}


async def code_exec_node(state: CodeActState) -> dict[str, Any]:
    """在进程隔离沙箱中执行代码。"""
    code = state.get("generated_code")
    if not code:
        return {
            "sandbox_result": SandboxResult(success=False, error="未生成有效代码"),
        }

    report = state.get("fetch_report")
    data_context = _build_exec_context(report)

    sandbox = ProcessSandbox()
    result = await sandbox.execute(code, data_context)
    return {"sandbox_result": result}


async def validate_node(state: CodeActState) -> dict[str, Any]:
    """验证执行结果。"""
    sandbox = state.get("sandbox_result")
    confirmed = state.get("confirmed_plan")
    code = state.get("generated_code", "")

    if not sandbox or not sandbox.success:
        return {"validation_report": ValidationReport(passed=False, errors=["执行失败"])}

    criteria = confirmed.validation_criteria if confirmed else None
    if not criteria:
        return {"validation_report": ValidationReport(passed=True)}

    formula = confirmed.plan_card.formula_description if confirmed else ""
    report = validate_result(
        result=sandbox.result,
        stats=sandbox.stats,
        criteria=criteria,
        formula_description=formula,
        code=code or "",
    )
    return {"validation_report": report}


async def repair_node(state: CodeActState) -> dict[str, Any]:
    """修复执行失败的代码。"""
    code = state.get("generated_code", "")
    sandbox = state.get("sandbox_result")
    error = sandbox.error if sandbox else "未知错误"
    report = state.get("fetch_report")

    successful_results = [r for r in (report.results if report else []) if r.success]

    repaired = await repair_code(
        code=code or "",
        error=error or "未知错误",
        fetch_results=successful_results,
    )

    return {
        "generated_code": repaired,
        "code_repair_count": state.get("code_repair_count", 0) + 1,
    }


async def format_node(state: CodeActState) -> dict[str, Any]:
    """格式化最终结果。"""
    sandbox = state.get("sandbox_result")
    confirmed = state.get("confirmed_plan")
    validation = state.get("validation_report")

    if not sandbox or not sandbox.success:
        return {"messages": [AIMessage(content="计算未产出有效结果。")]}

    question = _get_last_human_message(state)
    plan_summary = confirmed.summary() if confirmed else "无"

    prompt = FORMAT_RESULT_PROMPT.format(
        question=question,
        plan_summary=plan_summary,
        result=str(sandbox.result),
        stats=str(sandbox.stats),
    )

    llm = get_legacy_model()
    response = await llm.ainvoke([SystemMessage(content=prompt)])

    content = str(response.content)
    if validation and validation.warnings:
        content += "\n\n⚠ 验证提示: " + "; ".join(validation.warnings)

    return {"messages": [AIMessage(content=content)]}


async def hitl_fallback_node(state: CodeActState) -> dict[str, Any]:
    """执行异常回退到 HITL，报告错误并要求重新确认。"""
    reason = state.get("hitl_fallback_reason", "执行过程中出现异常")
    repair_count = state.get("code_repair_count", 0)

    msg = f"执行过程中出现问题，需要您重新确认计划。\n\n原因: {reason}"
    if repair_count > 0:
        msg += f"\n（已尝试自动修复 {repair_count} 次）"
    msg += "\n\n建议: 请检查取数来源和筛选条件是否正确，或尝试更明确地描述需求。"

    return {
        "hitl_status": "pending",
        "messages": [AIMessage(content=msg)],
    }


async def final_fallback_node(state: CodeActState) -> dict[str, Any]:
    """最终降级节点。"""
    repair_count = state.get("code_repair_count", 0)
    sandbox = state.get("sandbox_result")
    error = sandbox.error if sandbox else "未知错误"

    msg = (
        f"抱歉，动态计算经过 {repair_count} 次尝试仍未成功。\n"
        f"错误信息: {error}\n\n"
        "建议:\n"
        "1. 尝试更明确地描述计算逻辑\n"
        "2. 将问题拆分为更简单的子问题\n"
        "3. 联系业务方确认指标口径"
    )
    return {"messages": [AIMessage(content=msg)]}


# ── 路由 ──────────────────────────────────────────────────────────────────────


def hitl_router(state: CodeActState) -> Literal["lock", "refine", "decompose"]:
    """HITL 阶段路由。"""
    action = state.get("user_action", "")
    if action == "confirm":
        return "lock"
    if action == "modify":
        return "refine"
    return "decompose"


def after_ingest_router(state: CodeActState) -> Literal["fetch", "hitl_fallback"]:
    """计划摄入后路由。"""
    if state.get("hitl_fallback_reason"):
        return "hitl_fallback"
    if state.get("ingested_plan"):
        return "fetch"
    return "hitl_fallback"


def after_fetch_router(state: CodeActState) -> Literal["code_gen", "hitl_fallback"]:
    """取数后路由。"""
    if state.get("hitl_fallback_reason"):
        return "hitl_fallback"
    report = state.get("fetch_report")
    if report and report.has_data:
        return "code_gen"
    return "hitl_fallback"


def after_exec_router(state: CodeActState) -> Literal["validate", "repair", "final_fallback"]:
    """代码执行后路由。"""
    sandbox = state.get("sandbox_result")
    if sandbox and sandbox.success:
        return "validate"

    config = get_agent_config()
    if state.get("code_repair_count", 0) < config.code_max_repair_rounds:
        return "repair"

    return "final_fallback"


def after_validate_router(state: CodeActState) -> Literal["format", "repair", "hitl_fallback"]:
    """验证后路由。"""
    validation = state.get("validation_report")
    if validation and validation.passed:
        return "format"

    if validation and validation.errors:
        type_errors = [e for e in validation.errors if "类型不匹配" in e or "值超出" in e]
        if type_errors:
            return "hitl_fallback"

    config = get_agent_config()
    if state.get("code_repair_count", 0) < config.code_max_repair_rounds:
        return "repair"

    return "hitl_fallback"


# ── 工具函数 ──────────────────────────────────────────────────────────────────


def _get_last_human_message(state: CodeActState) -> str:
    """从 state.messages 中提取最后的用户消息。"""
    for msg in reversed(state.get("messages", [])):
        if msg.type == "human":
            return str(msg.content)
    return ""


def _build_exec_context(report: FetchReport | None) -> dict[str, Any]:
    """将 FetchReport 转为沙箱可用的数据上下文。"""
    if not report:
        return {}

    ctx: dict[str, Any] = {}
    try:
        import ast as _ast

        import pandas as pd

        idx = 0
        for fr in report.results:
            if not fr.success:
                continue
            try:
                data = fr.data
                parsed = _ast.literal_eval(data)
                if isinstance(parsed, list):
                    ctx[f"df_{idx}"] = pd.DataFrame(parsed)
                elif isinstance(parsed, dict):
                    ctx[f"df_{idx}"] = pd.DataFrame([parsed])
                else:
                    ctx[f"df_{idx}"] = pd.DataFrame()
            except Exception:
                ctx[f"df_{idx}"] = pd.DataFrame()
            idx += 1

        ctx["pd"] = pd
    except ImportError:
        pass

    try:
        import numpy as np
        ctx["np"] = np
    except ImportError:
        pass

    return ctx


# ── 图构建 ─────────────────────────────────────────────────────────────────────


async def build_codeact_graph(database_url: str | None = None) -> Any:
    """构建 CodeAct Engine 图（HITL + 执行）。

    Args:
        database_url: 数据库 URL（为 None 时从配置读取）

    Returns:
        Compiled LangGraph
    """
    agent_config = get_agent_config()

    db_manager = await get_nl2sql_db_manager(
        database_url=database_url,
        schema=agent_config.nl2sql_db_schema,
    )
    all_tools = create_async_sql_tools(db_manager)

    builder: StateGraph = StateGraph(CodeActState)

    # HITL 节点
    builder.add_node("decompose", decompose_node)
    builder.add_node("refine", refine_node)
    builder.add_node("lock", lock_node)

    # 执行节点
    builder.add_node("ingest", ingest_node)
    builder.add_node("fetch", _create_fetch_node(all_tools))
    builder.add_node("code_gen", code_gen_node)
    builder.add_node("code_exec", code_exec_node)
    builder.add_node("validate", validate_node)
    builder.add_node("repair", repair_node)
    builder.add_node("format", format_node)
    builder.add_node("hitl_fallback", hitl_fallback_node)
    builder.add_node("final_fallback", final_fallback_node)

    # 边
    builder.add_edge(START, "decompose")

    # HITL 流程目前直接进入 lock（用户确认通过 API 异步处理）
    # 在实际前端集成时，decompose 输出后会暂停等待用户操作
    # 这里构建完整的图结构，运行时通过 interrupt 机制实现等待
    builder.add_edge("decompose", "lock")
    builder.add_edge("refine", "lock")

    builder.add_edge("lock", "ingest")
    builder.add_conditional_edges("ingest", after_ingest_router, {
        "fetch": "fetch",
        "hitl_fallback": "hitl_fallback",
    })
    builder.add_conditional_edges("fetch", after_fetch_router, {
        "code_gen": "code_gen",
        "hitl_fallback": "hitl_fallback",
    })
    builder.add_edge("code_gen", "code_exec")
    builder.add_conditional_edges("code_exec", after_exec_router, {
        "validate": "validate",
        "repair": "repair",
        "final_fallback": "final_fallback",
    })
    builder.add_conditional_edges("validate", after_validate_router, {
        "format": "format",
        "repair": "repair",
        "hitl_fallback": "hitl_fallback",
    })
    builder.add_edge("repair", "code_exec")
    builder.add_edge("format", END)
    builder.add_edge("hitl_fallback", END)
    builder.add_edge("final_fallback", END)

    graph = builder.compile(name="codeact_engine")
    logger.info("CodeAct Engine 图构建成功")
    return graph


async def get_default_codeact_graph() -> Any:
    """获取默认 CodeAct Engine 图单例。"""
    global _default_codeact_graph

    if _default_codeact_graph is not None:
        return _default_codeact_graph

    async with _default_codeact_graph_lock:
        if _default_codeact_graph is None:
            _default_codeact_graph = await build_codeact_graph()

    return _default_codeact_graph
