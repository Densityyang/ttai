"""GenData Agent 工具定义。"""

import json
from typing import Any

from langchain_core.tools import BaseTool, tool

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.store.database import DatabaseManager
from src.nl2sql.infra.store.qa_rag import get_qa_retriever
from src.nl2sql.tools.async_sql_tools import create_async_sql_tools


def _format_rag_items(items: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for idx, item in enumerate(items, start=1):
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        distance = item.get("distance")
        if isinstance(distance, float):
            chunks.append(f"参考问答 {idx} (distance={distance:.4f}):\\n{content}")
        else:
            chunks.append(f"参考问答 {idx}:\\n{content}")
    return "\\n\\n".join(chunks)


def create_gen_data_tools(db_manager: DatabaseManager) -> list[BaseTool]:
    """创建 GenData Agent 工具集。"""
    sql_tools = create_async_sql_tools(db_manager)
    agent_config = get_agent_config()

    @tool("rag_retrieve")
    async def rag_retrieve(query: str, k: int | None = None) -> str:
        """检索历史问答样例，辅助 SQL 生成与问题理解。

        Args:
            query: 检索查询语句，建议是重写后的业务问题
            k: 检索条数，默认使用系统配置
        """
        normalized_query = query.strip()
        if not normalized_query:
            return "错误: query 不能为空"

        top_k = k if isinstance(k, int) and k > 0 else agent_config.rag_top_k
        try:
            items = await get_qa_retriever().aretrieve_filtered(
                normalized_query,
                k=top_k,
                relevance_threshold=agent_config.rag_relevance_threshold,
            )
        except Exception as exc:
            return f"RAG 检索失败: {exc!s}"

        if not items:
            return json.dumps(
                {
                    "query": normalized_query,
                    "top_k": top_k,
                    "hit_count": 0,
                    "summary": "未检索到可用历史问答",
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "query": normalized_query,
                "top_k": top_k,
                "hit_count": len(items),
                "best_distance": items[0].get("distance"),
                "summary": _format_rag_items(items),
            },
            ensure_ascii=False,
        )

    return [*sql_tools, rag_retrieve]
