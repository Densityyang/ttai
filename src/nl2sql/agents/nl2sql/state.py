"""SQL Agent 状态定义"""

from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class SQLAgentState(TypedDict):
    """SQL Agent 状态（无状态子 Agent，不含记忆管理字段）"""

    messages: Annotated[list[AnyMessage], add_messages]
    rag_context_injected: NotRequired[bool]
