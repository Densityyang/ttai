"""Agentic RAG 预处理子图 -- Self-RAG / CRAG / Adaptive RAG 风格。

Phase 3 升级：
- 自适应路由（Fast / Standard / Deep 三路策略）
- CRAG 三级置信决策（Correct / Ambiguous / Incorrect）
- 经验记忆注入（Phase 2 ExperienceStore 集成）
- GraphRAG 深度增强（知识精炼补充）

并发安全设计：
- 每次 create_agentic_rag() 返回独立编译的图实例
- AgenticRagState 是 TypedDict，每次 ainvoke 创建独立副本
- inject_node 中的并发检索使用 asyncio.gather，无共享可变状态
- GraphRAG 访问 lru_cache 单例（只读），NetworkX 图的只读遍历是线程安全的
- ExperienceStore 的读取是线程安全的（dict 读取在 CPython GIL 下原子）
- 所有 LLM 调用均通过 get_llm() 获取独立实例，无跨请求状态
"""

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from src.core.settings import ROOT_DIR
from src.nl2sql.agents.sql_agent.adaptive_router import (
    RoutePath,
)
from src.nl2sql.agents.sql_agent.adaptive_router import (
    route as adaptive_route,
)
from src.nl2sql.agents.sql_agent.experience_store import get_experience_store
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.llm.factory import get_llm
from src.nl2sql.infra.store.qa_rag import get_qa_retriever
from src.nl2sql.infra.store.semantic_rag import get_semantic_retriever
from src.nl2sql.semantic.retrieval import retrieve_active_semantic

from .state import ExplorationResult

logger = logging.getLogger(__name__)

# ── 工具定义 ──────────────────────────────────────────────────────────────────


@tool("qa_retriever_tool")
async def qa_retriever_tool(query: str) -> str:
    """从历史问答（QA）库中检索可能有助于回答用户问题的相关 SQL 设计参考或问答记录。"""
    try:
        retriever = get_qa_retriever()
        items = await retriever.aretrieve_filtered(query)
        if not items:
            return "QA库中未找到相关参考。"
        lines = []
        for index, item in enumerate(items, start=1):
            lines.append(
                f"- 证据 {index} (distance={item.get('distance', 0):.4f}):\n{item.get('content', '')}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"检索QA库出错: {e}"


@tool("semantic_retriever_tool")
async def semantic_retriever_tool(query: str) -> str:
    """从语义层检索指定业务术语、指标公式、源表及过滤条件的详细定义。"""
    return await _semantic_rag_retrieve(query)


async def _semantic_direct_read() -> str:
    """直接读取 semantic.md 全文内容。"""
    return "direct semantic-file reads are disabled; publish an active semantic release first"

    try:
        config = get_agent_config()
        file_path = Path(config.rag_semantic_file_path).expanduser()
        if not file_path.is_absolute():
            file_path = (ROOT_DIR / file_path).resolve()
        if not file_path.exists():
            return f"语义层文件不存在: {file_path}"
        content = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
        return content if content.strip() else "语义层文件为空。"
    except Exception as e:
        return f"读取语义层文件出错: {e}"


async def _semantic_rag_retrieve(query: str) -> str:
    """基于 FAISS 向量相似度检索语义层。"""
    try:
        active_result = await retrieve_active_semantic(query)
        if active_result.degraded and not active_result.documents:
            return f"semantic retrieval degraded: {active_result.reason}"
        if not active_result.documents:
            return "no semantic evidence in the active release"
        evidence = "\n\n".join(
            f"- evidence {index} (release={active_result.release_id}, domain={item.metadata.get('domain', '')}):\n{item.content}"
            for index, item in enumerate(active_result.documents, start=1)
        )
        graph = "\n".join(f"- graph relation: {hint}" for hint in active_result.graph_hints)
        degradation = (
            f"semantic retrieval degraded: {active_result.reason}\n\n" if active_result.degraded else ""
        )
        return f"{degradation}{evidence}" + (f"\n\n{graph}" if graph else "")

        retriever = get_semantic_retriever()
        items = await retriever.aretrieve_filtered(query)
        if not items:
            return "语义层中未找到与查询相关的指标定义。"
        lines = []
        for index, item in enumerate(items, start=1):
            lines.append(
                f"- 证据 {index} (distance={item.get('distance', 0):.4f}, 业务域={item.get('domain', '')}):\n"
                f"{item.get('content', '')}"
            )
        return "\n\n".join(lines)
    except Exception as e:
        return f"检索语义层出错: {e}"


# ── 子图 State ─────────────────────────────────────────────────────────────────


class AgenticRagState(TypedDict):
    """Agentic RAG 子图状态（Phase 3 自适应增强版）。

    并发安全：每次图调用创建独立状态副本，无跨请求共享。
    """

    messages: Annotated[list[AnyMessage], add_messages]
    tool_calls_count: int
    rewrite_count: int
    graded_evidences: list[dict[str, Any]]
    current_query: str
    structured_response: NotRequired[ExplorationResult | None]
    # Phase 3 新增
    route_path: NotRequired[RoutePath]
    confidence_tier: NotRequired[str]  # "correct" / "ambiguous" / "incorrect"
    experience_context: NotRequired[str]
    graphrag_supplement: NotRequired[str]


# ── 节点实现 ───────────────────────────────────────────────────────────────────

# ── CRAG 三级置信阈值 ──────────────────────────────────────────────────────────
# TUNABLE: 以下阈值从 AgentConfig 读取，可通过环境变量或 .env 覆盖。
# 初始值基于 CRAG 论文推荐，建议在 benchmark 评测中用 grid search 确定最优区间。
# 具体配置项: CRAG_CORRECT_THRESHOLD / CRAG_AMBIGUOUS_THRESHOLD


def _get_crag_thresholds() -> tuple[float, float]:
    """从配置读取 CRAG 阈值（延迟读取，避免模块级副作用）。"""
    config = get_agent_config()
    return config.crag_correct_threshold, config.crag_ambiguous_threshold


# 为便于模块内引用保留常量别名（运行时从 config 读取）
CRAG_CORRECT_THRESHOLD: float = 0.7    # TUNABLE: 默认值，运行时被 config 覆盖
CRAG_AMBIGUOUS_THRESHOLD: float = 0.4  # TUNABLE: 默认值，运行时被 config 覆盖

_GRADE_SYSTEM_PROMPT = (
    "你是一个检索质量评估专家。\n"
    "给定用户的原始问题和一组检索到的证据片段，请对每条证据评分（0.0-1.0）：\n"
    "- 1.0 = 该证据直接包含回答问题所需的表名、字段、公式或 SQL 参考\n"
    "- 0.7 = 该证据高度相关，包含关键业务逻辑或计算口径\n"
    "- 0.5 = 该证据部分相关，包含一些有用的业务术语或间接线索\n"
    "- 0.3 = 该证据关联度较弱，仅有模糊的业务概念重叠\n"
    "- 0.0 = 该证据与问题完全无关\n\n"
    "请以 JSON 数组格式返回，每项包含 source、score、reason 三个字段。\n"
    "示例：[{\"source\": \"qa\", \"score\": 0.8, \"reason\": \"包含相关表名和计算公式\"}]"
)

_REWRITE_SYSTEM_PROMPT = (
    "你是一个查询重写专家。\n"
    "当前检索结果质量不佳，请根据用户原始问题和低分原因，重写查询词以提高检索命中率。\n"
    "策略：\n"
    "1. 用业务同义词替换模糊表达\n"
    "2. 补充可能的业务术语（如指标名、表名关键词）\n"
    "3. 如果问题过于复杂，拆分为更聚焦的子查询\n\n"
    "只返回重写后的查询文本，不要解释。"
)

_FINALIZE_PROMPT = (
    "你是一个负责整理前置探索情报的业务架构师。\n"
    "请根据历史对话中所有已被检索到的内容（包括可能不太完美的结果），\n"
    "尽力提取出任何有价值的表名、业务逻辑、公式或用法提示，整理成结构化的 ExplorationResult。\n"
    "如果确实没有任何有用的信息，请将 has_useful_info 设置为 False，table_names 和 usage_hints 留空即可。"
)


def _extract_question(messages: list[AnyMessage]) -> str:
    """从消息列表中提取用户问题。"""
    for msg in reversed(messages):
        if msg.type == "human":
            return str(msg.content)
    return ""


async def route_node(state: AgenticRagState) -> dict[str, Any]:
    """自适应路由：检测查询复杂度并决定检索策略。

    并发安全：adaptive_route() 是纯函数，无共享状态。
    ExperienceStore.search_similar_queries() 只读 dict（CPython GIL 下原子读）。
    """
    question = _extract_question(state["messages"])
    if not question:
        return {"route_path": "standard", "current_query": "", "experience_context": ""}

    # 检查经验记忆（只读访问，并发安全）
    store = get_experience_store()
    has_experience = bool(store.search_similar_queries(question, top_k=1))
    experience_ctx = store.format_experience_context(question) if has_experience else ""

    decision = adaptive_route(question, has_experience_match=has_experience)
    logger.info(
        "RAG 路由决策: path=%s, reason=%s, experience=%s",
        decision.path, decision.reason, has_experience,
    )

    return {
        "route_path": decision.path,
        "current_query": question,
        "experience_context": experience_ctx,
    }


async def inject_node(state: AgenticRagState) -> dict[str, Any]:
    """并发执行初始检索并将结果注入为消息链。

    并发安全：
    - qa_retriever_tool 和 semantic_retriever_tool 各自独立，不共享可变状态
    - asyncio.gather 确保异常传播，不会泄漏协程
    - 返回新的 dict（不修改传入 state）
    """
    question = state.get("current_query") or _extract_question(state["messages"])
    if not question:
        return {"messages": [], "tool_calls_count": 0, "rewrite_count": 0, "graded_evidences": []}

    qa_task = asyncio.create_task(qa_retriever_tool.ainvoke({"query": question}))
    sem_task = asyncio.create_task(semantic_retriever_tool.ainvoke({"query": question}))
    qa_result, sem_result = await asyncio.gather(qa_task, sem_task)

    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "qa_retriever_tool", "args": {"query": question}, "id": "call_qa_init"},
            {"name": "semantic_retriever_tool", "args": {"query": question}, "id": "call_sem_init"},
        ],
    )
    tool_msg_qa = ToolMessage(content=str(qa_result), tool_call_id="call_qa_init", name="qa_retriever_tool")
    tool_msg_sem = ToolMessage(content=str(sem_result), tool_call_id="call_sem_init", name="semantic_retriever_tool")

    return {
        "messages": [ai_msg, tool_msg_qa, tool_msg_sem],
        "tool_calls_count": 0,
        "rewrite_count": 0,
        "graded_evidences": [],
    }


async def grade_node(state: AgenticRagState) -> dict[str, Any]:
    """CRAG 三级置信评分节点。

    三级决策（TUNABLE: 阈值在模块顶部定义）：
    - Correct:   avg_score >= CRAG_CORRECT_THRESHOLD   → 直接使用
    - Ambiguous:  avg_score >= CRAG_AMBIGUOUS_THRESHOLD → 知识精炼（裁剪 + GraphRAG 补充）
    - Incorrect: avg_score <  CRAG_AMBIGUOUS_THRESHOLD  → 全面重写 / 降级

    并发安全：LLM 调用是独立的，无共享可变状态。
    """
    config = get_agent_config()
    llm = get_llm(model_name=config.rag_grader_model)

    question = state.get("current_query") or _extract_question(state["messages"])

    evidences_text = []
    for msg in state["messages"]:
        if isinstance(msg, ToolMessage) and msg.content:
            content = str(msg.content)
            if content and not content.startswith("检索") and not content.startswith("QA库中未找到"):
                evidences_text.append(content)

    if not evidences_text:
        return {"graded_evidences": [], "confidence_tier": "incorrect"}

    combined = "\n---\n".join(evidences_text)
    prompt_text = (
        f"用户问题：{question}\n\n"
        f"检索到的证据：\n{combined}\n\n"
        "请对上述证据逐条评分并返回 JSON 数组。"
    )

    graded: list[dict[str, Any]] = []
    try:
        response = await llm.ainvoke([
            SystemMessage(content=_GRADE_SYSTEM_PROMPT),
            SystemMessage(content=prompt_text),
        ])
        import json
        content = str(response.content).strip()
        start = content.find("[")
        end = content.rfind("]") + 1
        if start >= 0 and end > start:
            scores_raw = json.loads(content[start:end])
            graded = [
                {
                    "source": item.get("source", "unknown"),
                    "score": float(item.get("score", 0.0)),
                    "reason": item.get("reason", ""),
                }
                for item in scores_raw
                if isinstance(item, dict)
            ]
    except Exception as e:
        logger.warning("CRAG 评分失败，降级为 incorrect: %s", e)

    # 计算平均分并确定置信等级
    scores = [ev.get("score", 0.0) for ev in graded]
    avg_score = sum(scores) / len(scores) if scores else 0.0

    # TUNABLE: 三级阈值从配置读取
    correct_th, ambiguous_th = _get_crag_thresholds()
    if avg_score >= correct_th:
        tier = "correct"
    elif avg_score >= ambiguous_th:
        tier = "ambiguous"
    else:
        tier = "incorrect"

    logger.info(
        "CRAG 评分: avg=%.2f, tier=%s, evidences=%d",
        avg_score, tier, len(graded),
    )

    return {"graded_evidences": graded, "confidence_tier": tier}


async def rewrite_node(state: AgenticRagState) -> dict[str, Any]:
    """基于低分原因重写查询词。"""
    llm = get_llm()
    question = state.get("current_query") or _extract_question(state["messages"])

    low_score_reasons = [
        ev.get("reason", "")
        for ev in state.get("graded_evidences", [])
        if ev.get("score", 0) < get_agent_config().rag_grader_score_threshold
    ]
    reasons_text = "; ".join(r for r in low_score_reasons if r) or "证据与问题不够相关"

    prompt_text = (
        f"原始问题：{question}\n"
        f"低分原因：{reasons_text}\n\n"
        "请重写查询词。"
    )

    try:
        response = await llm.ainvoke([
            SystemMessage(content=_REWRITE_SYSTEM_PROMPT),
            SystemMessage(content=prompt_text),
        ])
        new_query = str(response.content).strip()
        if new_query:
            return {
                "current_query": new_query,
                "rewrite_count": state.get("rewrite_count", 0) + 1,
            }
    except Exception as e:
        logger.warning("查询重写失败: %s", e)

    return {"rewrite_count": state.get("rewrite_count", 0) + 1}


async def re_retrieve_node(state: AgenticRagState) -> dict[str, Any]:
    """使用重写后的查询词重新检索。"""
    query = state.get("current_query") or _extract_question(state["messages"])

    qa_task = asyncio.create_task(qa_retriever_tool.ainvoke({"query": query}))
    sem_task = asyncio.create_task(semantic_retriever_tool.ainvoke({"query": query}))
    qa_result, sem_result = await asyncio.gather(qa_task, sem_task)

    rewrite_round = state.get("rewrite_count", 1)
    ai_msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "qa_retriever_tool", "args": {"query": query}, "id": f"call_qa_rw{rewrite_round}"},
            {"name": "semantic_retriever_tool", "args": {"query": query}, "id": f"call_sem_rw{rewrite_round}"},
        ],
    )
    tool_msg_qa = ToolMessage(
        content=str(qa_result), tool_call_id=f"call_qa_rw{rewrite_round}", name="qa_retriever_tool"
    )
    tool_msg_sem = ToolMessage(
        content=str(sem_result), tool_call_id=f"call_sem_rw{rewrite_round}", name="semantic_retriever_tool"
    )

    return {"messages": [ai_msg, tool_msg_qa, tool_msg_sem]}


async def refine_node(state: AgenticRagState) -> dict[str, Any]:
    """CRAG Ambiguous 级知识精炼节点。

    当评分处于模糊区间时：
    1. 裁剪低分证据（只保留 score >= CRAG_AMBIGUOUS_THRESHOLD 的）
    2. 用 GraphRAG 补充关联信息
    3. 如果有经验记忆，注入作为补充证据

    并发安全：GraphRAG 读取是只读的（NetworkX 只读遍历线程安全）。
    """
    graded = state.get("graded_evidences", [])

    # 1. 裁剪低分证据
    # TUNABLE: 裁剪阈值与 Ambiguous 下限一致
    kept = [ev for ev in graded if ev.get("score", 0) >= CRAG_AMBIGUOUS_THRESHOLD]
    pruned_count = len(graded) - len(kept)
    if pruned_count > 0:
        logger.info("CRAG 精炼: 裁剪 %d 条低分证据", pruned_count)

    # 2. GraphRAG 补充
    graphrag_text = ""
    try:
        from src.nl2sql.infra.store.graph_rag import expand_with_graph_rag
        table_names = _extract_table_names_from_evidences(kept)
        if table_names:
            expansion = expand_with_graph_rag(table_names)
            parts = []
            if expansion.get("expanded_tables"):
                parts.append(f"关联表: {', '.join(expansion['expanded_tables'])}")
            if expansion.get("join_hints"):
                parts.append("Join 提示:\n" + "\n".join(expansion["join_hints"]))
            if expansion.get("descriptions"):
                for name, desc in expansion["descriptions"].items():
                    if desc:
                        parts.append(f"{name}: {desc}")
            graphrag_text = "\n".join(parts) if parts else ""
    except Exception as e:
        logger.warning("CRAG 精炼 GraphRAG 补充失败: %s", e)

    # 3. 经验记忆注入
    experience_ctx = state.get("experience_context", "")

    # 将补充信息注入为消息
    supplement_parts: list[str] = []
    if graphrag_text:
        supplement_parts.append(f"[GraphRAG 补充]\n{graphrag_text}")
    if experience_ctx:
        supplement_parts.append(f"[经验记忆]\n{experience_ctx}")

    new_messages: list[AnyMessage] = []
    if supplement_parts:
        supplement = "\n\n".join(supplement_parts)
        refine_id = f"call_refine_{state.get('rewrite_count', 0)}"
        ai_msg = AIMessage(
            content="",
            tool_calls=[{"name": "knowledge_refine", "args": {}, "id": refine_id}],
        )
        tool_msg = ToolMessage(
            content=supplement,
            tool_call_id=refine_id,
            name="knowledge_refine",
        )
        new_messages = [ai_msg, tool_msg]

    return {
        "messages": new_messages,
        "graded_evidences": kept,
        "graphrag_supplement": graphrag_text,
    }


async def degrade_node(state: AgenticRagState) -> dict[str, Any]:
    """降级：标记证据不足，下游进入全库探索模式。"""
    tier = state.get("confidence_tier", "incorrect")
    rewrite_count = state.get("rewrite_count", 0)

    reason = (
        f"CRAG 置信等级={tier}, 重写轮次={rewrite_count}, "
        "未获得高质量证据，进入全库探索模式"
    )
    return {
        "structured_response": ExplorationResult(
            has_useful_info=False,
            degrade_reason=reason,
        ),
    }


async def finalize_node(state: AgenticRagState) -> dict[str, Any]:
    """基于现有全部情报输出结构化 ExplorationResult。

    整合：评分证据 + GraphRAG 补充 + 经验记忆。
    """
    existing = state.get("structured_response")
    if existing is not None:
        return {"messages": [], "structured_response": existing}

    # 将经验记忆和 GraphRAG 补充注入到 finalize 提示中
    extra_context_parts: list[str] = []
    experience_ctx = state.get("experience_context", "")
    graphrag_supp = state.get("graphrag_supplement", "")
    if experience_ctx:
        extra_context_parts.append(f"\n[历史成功查询参考]\n{experience_ctx}")
    if graphrag_supp:
        extra_context_parts.append(f"\n[GraphRAG 关联信息]\n{graphrag_supp}")

    finalize_prompt = _FINALIZE_PROMPT
    if extra_context_parts:
        finalize_prompt += "\n\n额外参考信息：" + "\n".join(extra_context_parts)

    llm = get_llm().with_structured_output(ExplorationResult, method="function_calling")
    msgs = [SystemMessage(content=finalize_prompt)] + state["messages"]
    try:
        result: ExplorationResult = await llm.ainvoke(msgs)  # type: ignore[assignment]
    except Exception as e:
        logger.error("Agentic RAG finalize 输出结构化结果出错: %s", e)
        result = ExplorationResult(has_useful_info=False)

    graded = state.get("graded_evidences", [])
    if graded:
        result.evidence_scores = graded

    return {"messages": [], "structured_response": result}


# ── 条件路由 ───────────────────────────────────────────────────────────────────


def after_route_router(state: AgenticRagState) -> Literal["inject", "fast_finalize"]:
    """路由节点后分流：Fast Path 跳过完整 RAG 直接进入简化检索+finalize。"""
    path = state.get("route_path", "standard")
    if path == "fast":
        return "fast_finalize"
    return "inject"


def after_grade_router(
    state: AgenticRagState,
) -> Literal["finalize", "refine", "rewrite", "degrade"]:
    """CRAG 三级置信路由。

    Correct   → finalize（直接使用）
    Ambiguous → refine（知识精炼：裁剪 + GraphRAG 补充）
    Incorrect → rewrite / degrade（全面重写 / 降级）

    TUNABLE: 阈值在 CRAG_CORRECT_THRESHOLD / CRAG_AMBIGUOUS_THRESHOLD 定义。
    """
    config = get_agent_config()
    tier = state.get("confidence_tier", "incorrect")

    if tier == "correct":
        return "finalize"

    if tier == "ambiguous":
        return "refine"

    # tier == "incorrect"
    rewrite_count = state.get("rewrite_count", 0)
    if rewrite_count < config.rag_max_rewrite_rounds:
        return "rewrite"

    return "degrade"


# ── 辅助函数 ──────────────────────────────────────────────────────────────────


def _extract_table_names_from_evidences(graded: list[dict[str, Any]]) -> list[str]:
    """从评分证据的 reason 中提取可能的表名。"""
    import re
    table_names: list[str] = []
    for ev in graded:
        reason = ev.get("reason", "")
        # 匹配 snake_case 的表名模式
        matches = re.findall(r"\b([a-z][a-z0-9_]{2,}(?:_[a-z0-9]+)+)\b", reason)
        table_names.extend(matches)
    return list(dict.fromkeys(table_names))


async def fast_finalize_node(state: AgenticRagState) -> dict[str, Any]:
    """Fast Path 简化检索 + 直接 finalize。

    对简单查询做单次向量检索（不经过完整 Self-RAG 评分循环），
    直接输出 ExplorationResult。延迟优先。

    并发安全：与 inject_node 类似，使用 asyncio.gather。
    """
    question = state.get("current_query") or _extract_question(state["messages"])
    if not question:
        return {
            "structured_response": ExplorationResult(has_useful_info=False),
        }

    # 单次快速检索（不评分不重写）
    qa_task = asyncio.create_task(qa_retriever_tool.ainvoke({"query": question}))
    sem_task = asyncio.create_task(semantic_retriever_tool.ainvoke({"query": question}))
    qa_result, sem_result = await asyncio.gather(qa_task, sem_task)

    # 直接结构化输出
    experience_ctx = state.get("experience_context", "")
    combined = f"QA检索:\n{qa_result}\n\n语义层检索:\n{sem_result}"
    if experience_ctx:
        combined += f"\n\n{experience_ctx}"

    llm = get_llm().with_structured_output(ExplorationResult, method="function_calling")
    try:
        response = await llm.ainvoke([
            SystemMessage(content=_FINALIZE_PROMPT + f"\n\n额外参考：\n{combined}"),
        ])
        result = (
            response
            if isinstance(response, ExplorationResult)
            else ExplorationResult.model_validate(response)
        )
    except Exception as e:
        logger.warning("Fast finalize 失败: %s", e)
        result = ExplorationResult(has_useful_info=False)

    return {"structured_response": result}


# ── 图构建 ─────────────────────────────────────────────────────────────────────


def create_agentic_rag() -> Any:
    """构建 Adaptive RAG 子图（Phase 3 升级版）。

    图结构：

        START → route → [条件: fast / standard+deep]
          ├─ fast_finalize → END                         (Fast Path)
          └─ inject → grade → [CRAG 三级]
               ├─ finalize → END                         (Correct)
               ├─ refine → finalize → END                (Ambiguous)
               ├─ rewrite → re_retrieve → grade          (Incorrect, 可重试)
               └─ degrade → finalize → END               (Incorrect, 超限)

    并发安全保证：
    - 每次 create_agentic_rag() 返回新的编译图实例
    - 每次 ainvoke 使用独立的 state 副本
    - 所有节点函数无共享可变状态
    - 并发检索使用 asyncio.gather（协程级并发，非线程）
    - GraphRAG 只读访问（lru_cache 单例 + NetworkX 只读遍历）
    - ExperienceStore 读取在 CPython GIL 下是安全的
    """
    builder: StateGraph = StateGraph(AgenticRagState)

    # 节点注册
    builder.add_node("route", route_node)
    builder.add_node("fast_finalize", fast_finalize_node)
    builder.add_node("inject", inject_node)
    builder.add_node("grade", grade_node)
    builder.add_node("refine", refine_node)
    builder.add_node("rewrite", rewrite_node)
    builder.add_node("re_retrieve", re_retrieve_node)
    builder.add_node("degrade", degrade_node)
    builder.add_node("finalize", finalize_node)

    # 边: 入口 → 路由
    builder.add_edge(START, "route")

    # 边: 路由分流
    builder.add_conditional_edges(
        "route",
        after_route_router,
        {"inject": "inject", "fast_finalize": "fast_finalize"},
    )
    builder.add_edge("fast_finalize", END)

    # 边: 标准/深度路径
    builder.add_edge("inject", "grade")
    builder.add_conditional_edges(
        "grade",
        after_grade_router,
        {
            "finalize": "finalize",
            "refine": "refine",
            "rewrite": "rewrite",
            "degrade": "degrade",
        },
    )

    # 边: Ambiguous 精炼后进入 finalize
    builder.add_edge("refine", "finalize")

    # 边: Incorrect 重写循环
    builder.add_edge("rewrite", "re_retrieve")
    builder.add_edge("re_retrieve", "grade")

    # 边: 降级后仍进入 finalize（尽力提取）
    builder.add_edge("degrade", "finalize")

    # 边: 终止
    builder.add_edge("finalize", END)

    return builder.compile(name="agentic_rag")
