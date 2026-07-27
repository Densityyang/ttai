"""语义 SQL Agent 查询服务。"""

from collections.abc import AsyncGenerator

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from src.core.observer import create_monitored_config
from src.nl2sql.config.settings import get_agent_config

from .graph import get_sql_agent_graph
from .state import SqlAgentState


async def query_database_with_semantic_sql_agent(
    question: str,
    database_url: str | None = None,
    config: RunnableConfig | None = None,
    session_id: str | None = None,
) -> AsyncGenerator[object, None]:
    """使用语义 SQL Agent 进行自然语言查询（流式返回）。"""
    agent_config = get_agent_config()
    graph = await get_sql_agent_graph(database_url=database_url)

    initial_state: SqlAgentState = {
        "messages": [HumanMessage(content=question)],
    }

    monitored_config = create_monitored_config(session_id, config, run_name="semantic_sql_agent")
    monitored_config = {
        **monitored_config,
        "recursion_limit": agent_config.graph_recursion_limit,
    }

    async for step in graph.astream(initial_state, monitored_config, stream_mode="values"):
        yield step
