"""sql_agent 状态定义。"""

from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class ExplorationResult(BaseModel):
    """前置知识探索结果的结构化定型。"""

    has_useful_info: bool = Field(description="知识库或历史检索中是否有针对该问题的相关业务逻辑、使用定义或参考片段。如果没有实质性帮助，必须输出 False")
    table_names: list[str] = Field(default_factory=list, description="相关且确切必须用来解答该问题的物理表或视图名列表。请确保表名真实存在于上下文档案中")
    usage_hints: str = Field(default="", description="请精炼总结：这些表应该如何使用？有什么特定的过滤条件？有哪些计算公式或过滤规则？或者附上直接的参考 SQL 片段")
    evidence_scores: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Self-RAG 评分后的证据列表，每项含 content/source/score",
    )
    degrade_reason: str | None = Field(
        default=None,
        description="若触发降级，记录降级原因",
    )


class SqlAgentState(TypedDict):
    """顶层 SQL Agent Orchestrator 状态。
    此状态用于贯穿最终的两个子 Agent，使得 RAG 的结果能够跨状态传递。
    """

    messages: Annotated[list[AnyMessage], add_messages]
    rag_info: NotRequired[ExplorationResult]
