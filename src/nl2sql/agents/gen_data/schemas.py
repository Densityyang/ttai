"""GenData Agent 结构化输出 schema。"""

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


ContentBlock = TextBlock | ChartBlock | MetricCardBlock | TableBlock


class GenDataResponse(BaseModel):
    """GenData Agent 的结构化响应。"""

    blocks: list[ContentBlock] = Field(description="内容块列表")
