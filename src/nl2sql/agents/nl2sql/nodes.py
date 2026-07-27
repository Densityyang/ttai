"""SQL Agent 节点定义

无状态子 Agent：不含记忆压缩、调用计数等横切逻辑，
这些由 Supervisor 的 Middleware 统一管理。
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.observer.langfuse import get_langfuse_handler
from src.nl2sql.infra.store.qa_rag import get_qa_retriever

from .state import SQLAgentState

NodeOutput = dict[str, Any]
logger = logging.getLogger(__name__)


def _extract_latest_question(state: SQLAgentState) -> str:
    for message in reversed(state["messages"]):
        if isinstance(message, HumanMessage):
            content = message.content
            if isinstance(content, str):
                return content.strip()
            return str(content).strip()
    return ""


def _format_rag_context(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""

    chunks: list[str] = []
    for idx, item in enumerate(items, start=1):
        content = str(item.get("content", "")).strip()
        if not content:
            continue

        distance = item.get("distance")
        if isinstance(distance, float):
            chunks.append(f"参考问答 {idx} (distance={distance:.4f}):\n{content}")
        else:
            chunks.append(f"参考问答 {idx}:\n{content}")

    return "\n\n".join(chunks)


def create_llm_node(
    model_with_tools: Any, system_prompt: str
) -> Callable[[SQLAgentState, RunnableConfig], Awaitable[NodeOutput]]:
    """创建 LLM 调用节点（无状态，不含记忆压缩）"""

    async def llm_call(state: SQLAgentState, config: RunnableConfig) -> NodeOutput:
        agent_config = get_agent_config()
        messages: list[Any] = [SystemMessage(content=system_prompt)]

        # RAG 上下文注入（仅首次）
        if not state.get("rag_context_injected", False):
            question = _extract_latest_question(state)
            if question:
                rag_top_k: int | None = None
                try:
                    rag_top_k = agent_config.rag_top_k
                    qa_items = await get_qa_retriever().aretrieve_filtered(
                        question,
                        k=rag_top_k,
                        relevance_threshold=agent_config.rag_relevance_threshold,
                    )
                    rag_context = _format_rag_context(qa_items)
                    if rag_context:
                        messages.append(
                            SystemMessage(
                                content=(
                                    "以下是通过向量检索得到的历史问答，仅作 SQL 生成参考，"
                                    "不要机械照搬，必须以当前数据库实际 schema 和查询结果为准。\n\n"
                                    f"{rag_context}"
                                )
                            )
                        )
                except Exception:
                    logger.exception(
                        "RAG 检索失败，降级为无检索上下文继续执行",
                        extra={"question": question[:200], "rag_top_k": rag_top_k},
                    )

        messages.extend(state["messages"])

        langfuse_handler = get_langfuse_handler()
        if langfuse_handler:
            config = cast(RunnableConfig, {**config, "callbacks": [langfuse_handler]})

        response = await model_with_tools.ainvoke(messages, config=config)

        return {
            "messages": [response],
            "rag_context_injected": True,
        }

    return llm_call


def create_tool_node(
    tools: list[BaseTool],
) -> Callable[[SQLAgentState], Awaitable[NodeOutput]]:
    """创建工具执行节点"""
    tools_by_name = {tool.name: tool for tool in tools}

    async def tool_node(state: SQLAgentState) -> NodeOutput:
        last_message = state["messages"][-1]
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {"messages": []}

        results: list[ToolMessage] = []
        for index, tool_call in enumerate(last_message.tool_calls, start=1):
            tool_name = tool_call["name"]
            tool_call_id = tool_call.get("id") or f"tool_call_{index}_{tool_name}"
            raw_tool_args = tool_call.get("args")

            if tool_name not in tools_by_name:
                results.append(ToolMessage(
                    content=f"错误: 工具 '{tool_name}' 不存在",
                    tool_call_id=tool_call_id,
                ))
                continue

            try:
                observation = await cast(Any, tools_by_name[tool_name]).ainvoke(raw_tool_args)
                results.append(ToolMessage(content=str(observation), tool_call_id=tool_call_id))
            except Exception as e:
                results.append(ToolMessage(content=f"工具执行错误: {e}", tool_call_id=tool_call_id))

        return {"messages": results}

    return tool_node
