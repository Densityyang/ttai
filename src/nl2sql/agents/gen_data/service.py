"""GenData Agent 查询服务。"""

from collections.abc import AsyncGenerator

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from src.core.observer import create_monitored_config
from src.nl2sql.agents.gen_data.agent import get_gen_data_agent
from src.nl2sql.config.settings import get_agent_config


async def query_database_with_gen_data_agent(
    question: str,
    database_url: str | None = None,
    config: RunnableConfig | None = None,
    session_id: str | None = None,
) -> AsyncGenerator[object, None]:
    """使用 GenData Agent 进行自然语言查询（流式返回）。"""
    agent_config = get_agent_config()
    agent = await get_gen_data_agent(database_url=database_url)

    initial_state: dict[str, object] = {
        "messages": [HumanMessage(content=question)],
    }

    monitored_config = create_monitored_config(session_id, config, run_name="gen_data_agent")
    monitored_config = {
        **monitored_config,
        "recursion_limit": agent_config.graph_recursion_limit,
    }

    async for step in agent.astream(initial_state, monitored_config, stream_mode="values"):
        if "messages" in step and step["messages"]:
            step["messages"][-1].pretty_print()
        yield step
