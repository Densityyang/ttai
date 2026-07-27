"""SQL Agent 查询服务

无状态子 Agent 入口：每次调用都是干净上下文，不维护 thread_id。
此服务主要供 Supervisor 的 tool 调用，也可独立使用。
"""

from collections.abc import AsyncGenerator

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from src.core.observer import create_monitored_config
from src.nl2sql.infra.runtime.registry import get_or_create_sql_graph

from ...config.settings import get_agent_config
from .state import SQLAgentState


async def query_database(
    question: str,
    database_url: str | None = None,
    config: RunnableConfig | None = None,
    session_id: str | None = None,
) -> AsyncGenerator[object, None]:
    """使用自然语言查询数据库（流式返回）"""
    agent_config = get_agent_config()
    agent = await get_or_create_sql_graph(database_url=database_url)

    initial_state: SQLAgentState = {
        "messages": [HumanMessage(content=question)],
    }

    monitored_config = create_monitored_config(session_id, config, run_name="sql_agent")
    monitored_config = {
        **monitored_config,
        "recursion_limit": agent_config.graph_recursion_limit,
    }

    async for step in agent.astream(initial_state, monitored_config, stream_mode="values"):
        if "messages" in step and step["messages"]:
            step["messages"][-1].pretty_print()
        yield step
