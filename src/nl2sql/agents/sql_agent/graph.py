"""语义 SQL Agent 图构建。"""

import asyncio
import logging
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.store.database import get_nl2sql_db_manager
from src.nl2sql.tools.async_sql_tools import create_async_sql_tools

from .agentic_rag import create_agentic_rag
from .sql_generator import create_sql_generator
from .state import ExplorationResult, SqlAgentState

logger = logging.getLogger(__name__)

_default_semantic_sql_graph: Any | None = None
_default_semantic_sql_graph_lock = asyncio.Lock()


async def build_sql_agent_graph(
    database_url: str | None = None,
    top_k: int | None = None,
    enable_rag: bool | None = None,
) -> Any:
    """构建 Agentic SQL 代理图。

    Args:
        database_url: 自定义数据库连接 URL，为 None 时使用默认配置。
        top_k: SQL 返回最大行数，为 None 时使用配置默认值。
        enable_rag: 是否启用前置 Agentic RAG 节点（语义预检索），为 None 时使用环境配置默认值。
    """
    agent_config = get_agent_config()
    final_enable_rag = (
        enable_rag if enable_rag is not None else agent_config.enable_agentic_rag
    )

    logger.info(
        "开始构建 Agentic SQL 图, database_url=%s, enable_rag=%s",
        "自定义" if database_url else "默认",
        final_enable_rag,
    )

    db_manager = await get_nl2sql_db_manager(
        database_url=database_url,
        schema=agent_config.nl2sql_db_schema,
    )
    all_tools = create_async_sql_tools(db_manager)

    async def agentic_rag_node_wrapper(state: SqlAgentState) -> dict[str, Any]:
        """Agentic RAG 前置单发节点（Self-RAG 增强版）"""
        agent = create_agentic_rag()

        question_msgs = [
            msg for msg in state["messages"] if msg.type in ("human", "system")
        ]

        try:
            result = cast(
                dict[str, Any],
                await agent.ainvoke(cast(Any, {"messages": question_msgs})),
            )
            rag_info = result.get("structured_response")
        except Exception as e:
            logger.error(f"Agentic RAG 运行出错: {e}")
            rag_info = None

        return {"rag_info": rag_info}

    async def sql_generator_node_wrapper(state: SqlAgentState) -> dict[str, Any]:
        """SQL 生成与执行自治节点（显式修复环路）"""
        sql_graph = create_sql_generator(tools=all_tools)

        question_msgs = [
            msg for msg in state["messages"] if msg.type in ("human", "system")
        ]

        rag_info: ExplorationResult | None = (
            state.get("rag_info") if final_enable_rag else None
        )

        prefetched_schema: str | None = None
        if rag_info and rag_info.table_names:
            try:
                prefetched_schema = db_manager.get_schema_description(
                    rag_info.table_names
                )
            except Exception as e:
                logger.warning("预取表结构失败，将由 LLM 自行探索: %s", e)

        result = cast(
            dict[str, Any],
            await sql_graph.ainvoke(
                cast(Any, {
                    "messages": question_msgs,
                    "rag_info": rag_info,
                    "prefetched_schema": prefetched_schema,
                    "generated_sql": None,
                    "query_result": None,
                    "last_error": None,
                    "repair_count": 0,
                    "table_names_str": "",
                }),
            ),
        )

        new_msgs = result["messages"][len(question_msgs):]
        return {"messages": new_msgs}

    builder = StateGraph(SqlAgentState)

    builder.add_node("sql_generator", sql_generator_node_wrapper)

    if final_enable_rag:
        builder.add_node("agentic_rag", agentic_rag_node_wrapper)
        builder.add_edge(START, "agentic_rag")
        builder.add_edge("agentic_rag", "sql_generator")
    else:
        builder.add_edge(START, "sql_generator")

    builder.add_edge("sql_generator", END)

    graph = builder.compile(name="semantic_sql_agent")
    logger.info("语义 SQL Agent 图构建成功（enable_rag=%s）", final_enable_rag)
    return graph


async def get_default_semantic_sql_agent_graph(top_k: int | None = None) -> Any:
    """获取默认语义 SQL Agent 图单例。"""
    global _default_semantic_sql_graph

    if _default_semantic_sql_graph is not None:
        return _default_semantic_sql_graph

    async with _default_semantic_sql_graph_lock:
        if _default_semantic_sql_graph is None:
            _default_semantic_sql_graph = await build_sql_agent_graph(top_k=top_k)

    return _default_semantic_sql_graph


async def get_sql_agent_graph(
    database_url: str | None = None,
    top_k: int | None = None,
) -> Any:
    """获取语义 SQL Agent 图。"""
    if database_url:
        return await build_sql_agent_graph(database_url=database_url, top_k=top_k)
    return await get_default_semantic_sql_agent_graph(top_k=top_k)
