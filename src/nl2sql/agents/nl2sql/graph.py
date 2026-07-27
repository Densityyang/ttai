"""SQL Agent 图构建

无状态子 Agent：不注入 checkpointer，每次调用都是干净上下文。
记忆管理由 Supervisor 层负责。
"""

import asyncio
import logging
from typing import Any, cast

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.llm.factory import get_llm
from src.nl2sql.infra.store.database import get_nl2sql_db_manager
from src.nl2sql.tools.async_sql_tools import create_async_sql_tools

from .nodes import create_llm_node, create_tool_node
from .prompts import get_system_prompt
from .state import SQLAgentState

logger = logging.getLogger(__name__)

_default_sql_graph: Any | None = None
_default_sql_graph_top_k: int | None = None
_default_sql_graph_lock = asyncio.Lock()


async def build_sql_agent_graph(
    database_url: str | None = None,
    top_k: int | None = None,
) -> Any:
    """构建 SQL Agent 图（无状态，不含 checkpointer）。"""
    logger.info(f"开始构建 SQL Agent 图, database_url={'自定义' if database_url else '默认'}, top_k={top_k}")
    agent_config = get_agent_config()
    top_k = top_k or agent_config.sql_top_k

    db_manager = await get_nl2sql_db_manager(
        database_url=database_url,
        schema=agent_config.nl2sql_db_schema,
    )
    model = get_llm()

    tools = create_async_sql_tools(db_manager)
    model_with_tools = cast(Any, model).bind_tools(tools)

    system_prompt = get_system_prompt("PostgreSQL", top_k)

    def should_continue(state: SQLAgentState) -> str:
        last_message = state["messages"][-1]
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            return "tool_node"
        return END

    builder = StateGraph(SQLAgentState)

    builder.add_node("llm_call", create_llm_node(model_with_tools, system_prompt))  # type: ignore[arg-type]
    builder.add_node("tool_node", create_tool_node(tools))  # type: ignore[arg-type]
    builder.add_edge(START, "llm_call")
    builder.add_conditional_edges("llm_call", should_continue, {"tool_node": "tool_node", END: END})
    builder.add_edge("tool_node", "llm_call")

    graph = builder.compile(name="sql_agent")
    logger.info("SQL Agent 图构建成功")
    return graph


async def get_default_sql_agent_graph(top_k: int | None = None) -> Any:
    """获取默认 SQL Agent 图单例（进程内复用）。"""
    global _default_sql_graph, _default_sql_graph_top_k
    if _default_sql_graph is not None:
        if top_k is not None and _default_sql_graph_top_k is not None and top_k != _default_sql_graph_top_k:
            raise ValueError(
                f"default SQL graph already initialized with top_k={_default_sql_graph_top_k}, "
                f"cannot reuse with top_k={top_k}"
            )
        logger.debug("复用已存在的默认 SQL Agent 图")
        return _default_sql_graph

    async with _default_sql_graph_lock:
        if _default_sql_graph is None:
            logger.info("首次创建默认 SQL Agent 图单例")
            agent_config = get_agent_config()
            effective_top_k = top_k if top_k is not None else agent_config.sql_top_k
            _default_sql_graph = await build_sql_agent_graph(top_k=effective_top_k)
            _default_sql_graph_top_k = effective_top_k
        elif top_k is not None and _default_sql_graph_top_k is not None and top_k != _default_sql_graph_top_k:
            raise ValueError(
                f"default SQL graph already initialized with top_k={_default_sql_graph_top_k}, "
                f"cannot reuse with top_k={top_k}"
            )
    return _default_sql_graph


async def get_sql_agent_graph(
    database_url: str | None = None,
    top_k: int | None = None,
) -> Any:
    """获取 SQL Agent 图。

    - 无 database_url：复用默认单例图
    - 有 database_url：按需构建临时图，不进入全局缓存
    """
    if database_url:
        return await build_sql_agent_graph(database_url=database_url, top_k=top_k)
    return await get_default_sql_agent_graph(top_k=top_k)
