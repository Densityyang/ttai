"""运行时单例注册器。"""

from typing import Any

from src.nl2sql.agents.gen_data.agent import (
    get_default_gen_data_agent,
    get_gen_data_agent,
)
from src.nl2sql.agents.nl2sql.graph import get_default_sql_agent_graph, get_sql_agent_graph
from src.nl2sql.agents.sql_agent.graph import (
    get_default_semantic_sql_agent_graph,
)
from src.nl2sql.agents.sql_agent.graph import (
    get_sql_agent_graph as get_semantic_sql_agent_graph,
)
from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.llm.gateway import get_legacy_model


async def get_or_create_sql_graph(
    database_url: str | None = None,
    top_k: int | None = None,
) -> Any:
    """获取（或创建）SQL Agent 图。"""
    if database_url:
        return await get_sql_agent_graph(database_url=database_url, top_k=top_k)
    return await get_default_sql_agent_graph(top_k=top_k)


async def get_or_create_semantic_sql_graph(
    database_url: str | None = None,
    top_k: int | None = None,
) -> Any:
    """获取（或创建）语义 SQL Agent 图。"""
    if database_url:
        return await get_semantic_sql_agent_graph(database_url=database_url, top_k=top_k)
    return await get_default_semantic_sql_agent_graph(top_k=top_k)


async def get_or_create_gen_data_agent(
    database_url: str | None = None,
    top_k: int | None = None,
) -> Any:
    """获取（或创建）GenData Agent。"""
    if database_url:
        return await get_gen_data_agent(database_url=database_url, top_k=top_k)
    return await get_default_gen_data_agent(top_k=top_k)


async def get_or_create_dynamic_calc_graph(
    database_url: str | None = None,
) -> Any:
    """获取（或创建）动态指标计算 Agent 图（旧版，保留兼容）。"""
    from src.nl2sql.agents.dynamic_calc.graph import (
        build_dynamic_calc_graph,
        get_default_dynamic_calc_graph,
    )

    if database_url:
        return await build_dynamic_calc_graph(database_url=database_url)
    return await get_default_dynamic_calc_graph()


async def get_or_create_codeact_graph(
    database_url: str | None = None,
) -> Any:
    """获取（或创建）CodeAct Engine 图（新版 HITL + 动态计算）。"""
    from src.nl2sql.agents.codeact_engine.graph import (
        build_codeact_graph,
        get_default_codeact_graph,
    )

    if database_url:
        return await build_codeact_graph(database_url=database_url)
    return await get_default_codeact_graph()


async def warmup_runtime() -> None:
    """预热默认运行时对象。"""
    get_legacy_model()
    await get_or_create_semantic_sql_graph()

    config = get_agent_config()
    if config.enable_dynamic_calc:
        await get_or_create_dynamic_calc_graph()

    if config.enable_graph_rag:
        from src.nl2sql.infra.store.graph_rag import get_schema_relation_graph
        get_schema_relation_graph()
