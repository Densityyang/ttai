"""GenData Agent 中间件。"""

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from src.nl2sql.config.settings import get_agent_config
from src.nl2sql.infra.store.database import DatabaseManager
from src.nl2sql.infra.store.qa_rag import get_qa_retriever

logger = logging.getLogger(__name__)


class PrefetchRAGAndSchemaMiddleware(AgentMiddleware[Any, Any]):
    """在 before_agent 阶段预取 RAG 与可用表列表。"""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db_manager = db_manager

    async def abefore_agent(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        del runtime
        messages_raw = state.get("messages")
        if not isinstance(messages_raw, list):
            return None

        messages = [msg for msg in messages_raw if isinstance(msg, BaseMessage)]
        question = self._extract_latest_question(messages)
        if not question:
            return None

        context_blocks: list[str] = []
        rag_context = await self._build_rag_context(question)
        if rag_context:
            context_blocks.append(rag_context)

        schema_context = self._build_schema_context()
        if schema_context:
            context_blocks.append(schema_context)

        if not context_blocks:
            return None

        injected_message = SystemMessage(content="\n\n".join(context_blocks))
        return {"messages": [*messages, injected_message]}

    @staticmethod
    def _extract_latest_question(messages: list[BaseMessage]) -> str:
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                content = message.content
                if isinstance(content, str):
                    return content.strip()
                return str(content).strip()
        return ""

    async def _build_rag_context(self, question: str) -> str:
        config = get_agent_config()
        try:
            items = await get_qa_retriever().aretrieve_filtered(
                question,
                k=config.rag_top_k,
                relevance_threshold=config.rag_relevance_threshold,
            )
        except Exception:
            logger.exception("预先 RAG 检索失败，降级继续", extra={"question": question[:200]})
            return ""

        if not items:
            return ""

        lines = [
            "以下是预先检索到的历史问答（仅供参考，必须以当前数据库 schema 与查询结果为准）："
        ]
        for idx, item in enumerate(items, start=1):
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            distance = item.get("distance")
            if isinstance(distance, float):
                lines.append(f"参考问答 {idx} (distance={distance:.4f}):\\n{content}")
            else:
                lines.append(f"参考问答 {idx}:\\n{content}")
        return "\n\n".join(lines)

    def _build_schema_context(self) -> str:
        try:
            table_names = self._db_manager.get_table_names()
            if not table_names:
                return ""
        except Exception:
            logger.exception("预先可用表列表获取失败，降级继续")
            return ""

        table_list = ", ".join(table_names)
        return (
            "以下是当前数据库可用表（仅表名，未预取表结构）：\n"
            f"{table_list}\n"
            "如需具体字段/类型/主键信息，请先筛选相关表，再调用 `sql_db_schema` 按需获取。"
        )
