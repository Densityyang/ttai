"""Supervisor and canonical public response-block contracts."""

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _PublicBlock(BaseModel):
    """Canonical Foundation-C block: strict and server serializable."""

    model_config = ConfigDict(extra="forbid")


class TextBlock(_PublicBlock):
    """文本内容块。"""
    type: Literal["text"] = "text"
    text: str

class ChartBlock(_PublicBlock):
    """图表内容块。"""
    type: Literal["chart"] = "chart"
    chart_type: str
    title: str
    echarts_option: dict[str, Any]

class MetricCardBlock(_PublicBlock):
    """指标卡片内容块。"""
    type: Literal["metric_card"] = "metric_card"
    label: str
    value: str | int | float
    unit: str | None = Field(description="指标的单位")
    trend: Literal["up", "down", "flat"] | None = None
    change_rate: str | None = None

class TableBlock(_PublicBlock):
    """表格内容块。"""
    type: Literal["table"] = "table"
    title: str
    columns: list[str]
    rows: list[list[str | int | float | None]]

class ImageBlock(_PublicBlock):
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


class ClarificationBlock(_PublicBlock):
    """Safe projection of one server-owned pending HITL request."""

    type: Literal["clarification"] = "clarification"
    request_id: str = Field(min_length=1, max_length=128)
    decision_kind: Literal[
        "clarification", "business_confirmation", "risk_policy_decision"
    ]
    version: int = Field(ge=1, le=1_000_000)
    allowed_actions: tuple[
        Literal["resolve", "confirm", "modify", "choose", "reject", "cancel"], ...
    ] = Field(min_length=1, max_length=6)
    unresolved_slots: tuple[str, ...] = Field(default=(), max_length=16)
    issue_codes: tuple[str, ...] = Field(default=(), max_length=32)
    safe_summary: str | None = Field(default=None, max_length=512)


class PlanCardBlock(_PublicBlock):
    """Canonical plan card; distinct from the legacy internal HITL block."""

    type: Literal["plan_card"] = "plan_card"
    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=4_096)
    version: int = Field(ge=1)
    actions: tuple[str, ...] = Field(default=(), max_length=8)


class ModeSuggestionBlock(_PublicBlock):
    type: Literal["mode_suggestion"] = "mode_suggestion"
    current_mode: Literal["QUERY", "ANALYZE", "BUILD"]
    suggested_mode: Literal["QUERY", "ANALYZE", "BUILD"]
    outcome: Literal["cannot_resolve", "clarification_required"]
    reason: str = Field(min_length=1, max_length=512)
    run_id: str = Field(min_length=1, max_length=128)


class ConflictLineageBlock(_PublicBlock):
    identity_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    derived_from_identity: str | None = Field(default=None, max_length=128)
    derived_from_version: int | None = Field(default=None, ge=1)
    source_definition_id: str = Field(min_length=1, max_length=128)
    source_definition_version: int = Field(ge=1)
    source_definition_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConflictCandidateBlock(_PublicBlock):
    candidate_id: str = Field(min_length=1, max_length=128)
    definition_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=256)
    origin: Literal["own", "installed"]
    owner_label: str | None = Field(default=None, max_length=128)
    source_label: str | None = Field(default=None, max_length=256)
    certification_state: Literal["unknown", "uncertified", "certified"]
    star_count: int = Field(ge=0)
    semantic_difference_kinds: tuple[str, ...] = Field(default=(), max_length=16)
    lineage: ConflictLineageBlock
    risk_notes: tuple[str, ...] = Field(default=(), max_length=16)


class ConflictComparisonBlock(_PublicBlock):
    type: Literal["conflict_comparison"] = "conflict_comparison"
    conflict_id: str = Field(min_length=1, max_length=128)
    required_slot: str = Field(min_length=1, max_length=64)
    candidates: tuple[ConflictCandidateBlock, ...] = Field(min_length=2, max_length=16)


class ProvenanceAuthorityBlock(_PublicBlock):
    execution_plan_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_step_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    source_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_checkpoints: tuple[str, ...] = Field(default=(), max_length=16)
    semantic_signatures: tuple[str, ...] = Field(default=(), max_length=16)


class ProvenanceTimeRangeBlock(_PublicBlock):
    start: str
    end: str
    timezone: str = Field(min_length=1, max_length=128)


class ProvenanceBlock(_PublicBlock):
    type: Literal["provenance"] = "provenance"
    evidence_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_keys: tuple[str, ...] = Field(min_length=1, max_length=16)
    analysis_window: ProvenanceTimeRangeBlock
    data_as_of: datetime | None = None
    data_as_of_date: date | None = None
    source_ids: tuple[str, ...] = Field(default=(), max_length=16)
    source_checkpoints: tuple[str, ...] = Field(default=(), max_length=16)
    fact_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    authority_provenance: ProvenanceAuthorityBlock
    model_provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_model_pair(self) -> "ProvenanceBlock":
        if (self.model_provider is None) != (self.model is None):
            raise ValueError("model provenance must provide provider and model together")
        return self


class DefinitionBlock(_PublicBlock):
    type: Literal["definition"] = "definition"
    definition_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=256)
    confirmation: Literal["DRAFT", "CONFIRMED"]
    retention: Literal["SESSION", "SAVED"]
    publication: Literal["UNPUBLISHED", "PUBLISHED"]
    certification: Literal["UNCERTIFIED", "CERTIFIED"]
    semantic_closed: bool
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

ResponseBlock = (
    TextBlock | ChartBlock | MetricCardBlock | TableBlock
    | ImageBlock | ClarificationBlock | PlanCardBlock | ModeSuggestionBlock
    | ConflictComparisonBlock | ProvenanceBlock | DefinitionBlock
    | CodeResultBlock | HITLPlanCardBlock | HITLConfirmationBlock
)

PUBLIC_BLOCK_MODELS: Final[dict[str, type[_PublicBlock]]] = {
    "text": TextBlock,
    "chart": ChartBlock,
    "metric_card": MetricCardBlock,
    "table": TableBlock,
    "image": ImageBlock,
    "clarification": ClarificationBlock,
    "plan_card": PlanCardBlock,
    "mode_suggestion": ModeSuggestionBlock,
    "conflict_comparison": ConflictComparisonBlock,
    "provenance": ProvenanceBlock,
    "definition": DefinitionBlock,
}

_LEGACY_BLOCK_MODELS: Final[dict[str, type[BaseModel]]] = {
    "code_result": CodeResultBlock,
    "hitl_plan_card": HITLPlanCardBlock,
    "hitl_confirmation": HITLConfirmationBlock,
}

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
            raw_type = block.get("type")
            model = (
                PUBLIC_BLOCK_MODELS.get(raw_type)
                if isinstance(raw_type, str)
                else None
            )
            legacy = (
                _LEGACY_BLOCK_MODELS.get(raw_type)
                if isinstance(raw_type, str)
                else None
            )
            if model is not None:
                serialized.append(model.model_validate(block).model_dump())
            elif legacy is not None:
                serialized.append(legacy.model_validate(block).model_dump())
            else:
                from src.nl2sql.v2 import UNSUPPORTED_BLOCK_FALLBACK

                serialized.append(
                    {
                        "type": (
                            raw_type[:128]
                            if isinstance(raw_type, str) and raw_type
                            else "unknown"
                        ),
                        "fallback": dict(UNSUPPORTED_BLOCK_FALLBACK),
                    }
                )
            continue
        raise TypeError(f"Unsupported block type: {type(block)}")
    return serialized


__all__ = [
    "ChartBlock",
    "ClarificationBlock",
    "CodeResultBlock",
    "ConflictCandidateBlock",
    "ConflictComparisonBlock",
    "DefinitionBlock",
    "HITLConfirmationBlock",
    "HITLPlanCardBlock",
    "ImageBlock",
    "MetricCardBlock",
    "ModeSuggestionBlock",
    "PUBLIC_BLOCK_MODELS",
    "PlanCardBlock",
    "ProvenanceAuthorityBlock",
    "ProvenanceBlock",
    "ProvenanceTimeRangeBlock",
    "ResponseBlock",
    "SupervisorResponse",
    "TableBlock",
    "TextBlock",
    "serialize_response_blocks",
]
