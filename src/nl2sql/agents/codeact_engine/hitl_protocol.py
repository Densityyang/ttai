"""HITL 确认协议 -- 计算计划的人机协作确认流程。

核心理念：先对齐需求，再自主执行。
动态指标计算的最大失败来源是用户需求定义不清，而非模型能力不足。
"""

import logging
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import SystemMessage

from src.nl2sql.agents.codeact_engine.plan_card import (
    CalcPlanCard,
    ComputeStep,
    ConfirmedCalcPlan,
    DataFetchStep,
    ValidationCriteria,
)
from src.nl2sql.agents.codeact_engine.prompts import (
    DECOMPOSER_SYSTEM_PROMPT,
    PLAN_REFINE_SYSTEM_PROMPT,
)
from src.nl2sql.infra.llm.gateway import get_legacy_model

logger = logging.getLogger(__name__)

MAX_REFINEMENT_ROUNDS = 3

# Confidence threshold below which we inject additional warnings
LOW_CONFIDENCE_THRESHOLD = 0.6


async def decompose_to_plan_card(
    question: str,
    schema_context: str = "",
) -> CalcPlanCard:
    """将自然语言需求分解为结构化 CalcPlanCard。

    Args:
        question: 用户的自然语言描述
        schema_context: 可用的表结构/语义层上下文

    Returns:
        结构化的 CalcPlanCard，包含 AI 对四要素的分解结果
    """
    llm = get_legacy_model().with_structured_output(CalcPlanCard, method="function_calling")

    prompt = DECOMPOSER_SYSTEM_PROMPT.format(schema_context=schema_context or "（无可用 schema 信息）")

    try:
        response = await llm.ainvoke([
            SystemMessage(content=prompt),
            SystemMessage(content=f"用户需求：{question}"),
        ])
        card = response if isinstance(response, CalcPlanCard) else CalcPlanCard.model_validate(response)
    except Exception:
        logger.exception("CalcPlanCard 生成失败")
        card = CalcPlanCard(
            data_sources=[],
            source_confidence=0.0,
            filters=[],
            data_flow="无法解析用户需求",
            computation_steps=[],
            formula_description="",
            intermediate_outputs=[],
            output_type="未知",
            output_precision="未知",
            output_unit="未知",
            output_format="未知",
            ambiguity_warnings=["AI 无法理解当前描述，请更详细地描述您的需求"],
        )

    if card.source_confidence < LOW_CONFIDENCE_THRESHOLD and not card.ambiguity_warnings:
        card.ambiguity_warnings.append(
            f"AI 对取数来源的置信度较低 ({card.source_confidence:.0%})，建议您仔细确认"
        )

    return card


async def refine_plan_card(
    current_card: CalcPlanCard,
    user_feedback: str,
    schema_context: str = "",
) -> CalcPlanCard:
    """根据用户反馈修正 CalcPlanCard。

    Args:
        current_card: 当前计划卡片
        user_feedback: 用户的修改意见
        schema_context: 可用的表结构/语义层上下文

    Returns:
        更新后的 CalcPlanCard
    """
    next_version = current_card.plan_version + 1

    if next_version > MAX_REFINEMENT_ROUNDS + 1:
        logger.warning("计划修正轮次超限 (v%d)，建议用户重新描述", next_version)
        current_card.ambiguity_warnings.append(
            f"已修改 {MAX_REFINEMENT_ROUNDS} 轮仍未达成一致，建议重新描述需求或联系业务方确认口径"
        )
        return current_card

    llm = get_legacy_model().with_structured_output(CalcPlanCard, method="function_calling")
    prompt = PLAN_REFINE_SYSTEM_PROMPT.format(
        current_plan_markdown=current_card.to_markdown(),
        user_feedback=user_feedback,
        next_version=next_version,
    )

    try:
        response = await llm.ainvoke([
            SystemMessage(content=prompt),
        ])
        refined = (
            response
            if isinstance(response, CalcPlanCard)
            else CalcPlanCard.model_validate(response)
        )
        refined.plan_version = next_version
        return refined
    except Exception:
        logger.exception("CalcPlanCard 修正失败")
        current_card.ambiguity_warnings.append("修正过程出错，请重试")
        return current_card


def lock_plan(
    card: CalcPlanCard,
    user_id: str = "user",
    modification_history: list[str] | None = None,
) -> ConfirmedCalcPlan:
    """将 CalcPlanCard 锁定为 ConfirmedCalcPlan。

    从 CalcPlanCard 派生出精确的取数指令和计算指令。
    """
    data_fetch_instructions = []
    for i, ds in enumerate(card.data_sources):
        relevant_filters = [f for f in card.filters]
        data_fetch_instructions.append(
            DataFetchStep(
                step_id=i + 1,
                source=ds,
                filters=relevant_filters,
                description=f"从 {ds.table} 获取 {', '.join(ds.fields) if ds.fields else '所有字段'}",
                expected_columns=ds.fields,
            )
        )

    compute_instructions = [
        ComputeStep(
            step_id=step.step_id,
            description=step.description,
            expected_output=step.expected_output,
            depends_on=step.depends_on,
        )
        for step in card.computation_steps
    ]

    validation = _derive_validation_criteria(card)

    return ConfirmedCalcPlan(
        plan_card=card,
        confirmed_at=datetime.now(tz=timezone.utc),
        confirmed_by=user_id,
        modification_history=modification_history or [],
        is_locked=True,
        data_fetch_instructions=data_fetch_instructions,
        compute_instructions=compute_instructions,
        validation_criteria=validation,
    )


def _derive_validation_criteria(card: CalcPlanCard) -> ValidationCriteria:
    """从 CalcPlanCard 推导结果验证标准。"""
    output_type_lower = card.output_type.lower()

    expected_type = "Any"
    value_range = None
    precision = None
    allow_null = False

    if "比率" in output_type_lower or "率" in output_type_lower:
        expected_type = "float"
        if "%" in card.output_unit:
            value_range = (0.0, 100.0)
        else:
            value_range = (0.0, 1.0)
    elif "数值" in output_type_lower or "个数" in output_type_lower or "数量" in output_type_lower:
        expected_type = "int"
    elif "排名" in output_type_lower or "表" in output_type_lower:
        expected_type = "DataFrame"
    elif "列表" in output_type_lower:
        expected_type = "list"

    precision_str = card.output_precision
    if "小数" in precision_str:
        import re
        match = re.search(r"(\d+)", precision_str)
        if match:
            precision = int(match.group(1))
    elif "整数" in precision_str:
        precision = 0

    return ValidationCriteria(
        expected_type=expected_type,
        value_range=value_range,
        allow_null=allow_null,
        precision=precision,
        unit=card.output_unit if card.output_unit != "未知" else None,
    )


def render_plan_for_chat(card: CalcPlanCard) -> dict[str, Any]:
    """将 CalcPlanCard 渲染为前端可消费的消息结构。

    Returns:
        dict 包含:
        - type: "hitl_plan_card"
        - plan_card: 完整卡片数据
        - markdown: Markdown 降级渲染
        - actions: 可用操作列表
    """
    can_confirm = (
        len(card.data_sources) > 0
        and card.formula_description
        and card.output_type != "未知"
    )

    over_limit = card.plan_version > MAX_REFINEMENT_ROUNDS
    actions = ["confirm", "modify", "restart"]
    if over_limit:
        actions = ["confirm", "restart"]

    return {
        "type": "hitl_plan_card",
        "plan_card": card.model_dump(),
        "markdown": card.to_markdown(),
        "actions": actions,
        "can_confirm": can_confirm,
        "version": card.plan_version,
        "ambiguity_count": len(card.ambiguity_warnings),
    }
