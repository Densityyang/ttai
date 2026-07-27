"""GenData create_agent 构建与获取。"""

import asyncio
import logging
from typing import Any

from langchain.agents import create_agent

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.llm.factory import get_llm
from src.nl2sql.infra.store.database import get_nl2sql_db_manager

from .middleware import PrefetchRAGAndSchemaMiddleware
from .prompts import get_system_prompt
from .tools import create_gen_data_tools

logger = logging.getLogger(__name__)

_default_gen_data_agent: Any | None = None
_default_gen_data_agent_top_k: int | None = None
_default_gen_data_agent_lock = asyncio.Lock()


async def build_gen_data_agent(
    database_url: str | None = None,
    top_k: int | None = None,
) -> Any:
    """构建 GenData create_agent 实例。"""
    logger.info(
        "开始构建 GenData create_agent, database_url=%s, top_k=%s",
        "自定义" if database_url else "默认",
        top_k,
    )
    agent_config = get_agent_config()
    effective_top_k = top_k if top_k is not None else agent_config.sql_top_k

    db_manager = await get_nl2sql_db_manager(
        database_url=database_url,
        schema=agent_config.nl2sql_db_schema,
    )
    model = get_llm()

    tools = create_gen_data_tools(db_manager)
    middleware = [PrefetchRAGAndSchemaMiddleware(db_manager=db_manager)]

    agent = create_agent(
        model=model,
        tools=tools,
        middleware=middleware,
        system_prompt=get_system_prompt("PostgreSQL", effective_top_k),
        name="gen_data_agent",
    )
    logger.info("GenData create_agent 构建成功")
    return agent


async def get_default_gen_data_agent(top_k: int | None = None) -> Any:
    """获取默认 GenData create_agent 单例。"""
    global _default_gen_data_agent, _default_gen_data_agent_top_k
    if _default_gen_data_agent is not None:
        if (
            top_k is not None
            and _default_gen_data_agent_top_k is not None
            and top_k != _default_gen_data_agent_top_k
        ):
            raise ValueError(
                f"default GenData agent already initialized with top_k={_default_gen_data_agent_top_k}, "
                f"cannot reuse with top_k={top_k}"
            )
        return _default_gen_data_agent

    async with _default_gen_data_agent_lock:
        if _default_gen_data_agent is None:
            agent_config = get_agent_config()
            effective_top_k = top_k if top_k is not None else agent_config.sql_top_k
            _default_gen_data_agent = await build_gen_data_agent(top_k=effective_top_k)
            _default_gen_data_agent_top_k = effective_top_k
        elif (
            top_k is not None
            and _default_gen_data_agent_top_k is not None
            and top_k != _default_gen_data_agent_top_k
        ):
            raise ValueError(
                f"default GenData agent already initialized with top_k={_default_gen_data_agent_top_k}, "
                f"cannot reuse with top_k={top_k}"
            )
    return _default_gen_data_agent


async def get_gen_data_agent(
    database_url: str | None = None,
    top_k: int | None = None,
) -> Any:
    """获取 GenData create_agent。"""
    if database_url:
        return await build_gen_data_agent(database_url=database_url, top_k=top_k)
    return await get_default_gen_data_agent(top_k=top_k)
