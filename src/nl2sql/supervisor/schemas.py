"""Supervisor Agent 的结构化输出 schema。"""

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field


class TextBlock(BaseModel):
    """文本内容块。"""
    type: Literal["text"] = "text"
    text: str

class ChartBlock(BaseModel):
    """图表内容块。"""
    type: Literal["chart"] = "chart"
    chart_type: str
    title: str
    echarts_option: dict[str, Any]

class MetricCardBlock(BaseModel):
    """指标卡片内容块。"""
    type: Literal["metric_card"] = "metric_card"
    label: str
    value: str | int | float
    unit: str | None = Field(description="指标的单位")
    trend: Literal["up", "down", "flat"] | None = None
    change_rate: str | None = None

class TableBlock(BaseModel):
    """表格内容块。"""
    type: Literal["table"] = "table"
    title: str
    columns: list[str]
    rows: list[list[str | int | float | None]]

class ImageBlock(BaseModel):
    """图片内容块。"""
    type: Literal["image"] = "image"
    url: str
    alt: str | None = None

class CodeResultBlock(BaseModel):
    """动态代码计算结果块。"""
    type: Literal["code_result"] = "code_result"
    title: str
    final_value: str | int | float | None = None
    intermediate_stats: dict[str, Any] = Field(default_factory=dict)
    execution_summary: str = ""
    code_snippet: str | None = Field(
        default=None, description="执行的代码片段（脱敏后）"
    )

class HITLPlanCardBlock(BaseModel):
    """HITL 计算计划卡片块 -- 展示给用户确认。"""
    type: Literal["hitl_plan_card"] = "hitl_plan_card"
    plan_card: dict[str, Any] = Field(description="CalcPlanCard 完整数据")
    markdown: str = Field(description="Markdown 降级渲染")
    actions: list[str] = Field(
        default_factory=lambda: ["confirm", "modify", "restart"],
        description="可用操作: confirm / modify / restart",
    )
    can_confirm: bool = Field(default=True, description="是否可以确认执行")
    version: int = Field(default=1, description="计划版本号")
    ambiguity_count: int = Field(default=0, description="歧义点数量")

class HITLConfirmationBlock(BaseModel):
    """HITL 确认状态块。"""
    type: Literal["hitl_confirmation"] = "hitl_confirmation"
    status: str = Field(description="confirmed / modified / rejected")
    plan_summary: str = Field(default="", description="确认计划摘要")
    modification_note: str = Field(default="", description="修改说明")

ResponseBlock = (
    TextBlock | ChartBlock | MetricCardBlock | TableBlock
    | ImageBlock | CodeResultBlock | HITLPlanCardBlock | HITLConfirmationBlock
)

class SupervisorResponse(BaseModel):
    """Supervisor Agent 的结构化响应。"""
    blocks: list[ResponseBlock] = Field(description="统一生成的响应内容块列表")


def serialize_response_blocks(blocks: Sequence[ResponseBlock] | Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """将内容块统一序列化为前端可消费的字典结构。"""
    serialized: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, BaseModel):
            serialized.append(block.model_dump())
            continue
        if isinstance(block, dict):
            serialized.append(block)
            continue
        raise TypeError(f"Unsupported block type: {type(block)}")
    return serialized
