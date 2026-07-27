"""计划摄入模块 -- 校验并拆解 ConfirmedCalcPlan 为可执行指令。

职责：
1. 校验计划完整性（必填字段、步骤依赖合法性）
2. 提取取数指令和计算指令
3. 如果计划本身有缺陷，抛出 PlanIngestionError 而非自行修改
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from src.nl2sql.agents.codeact_engine.plan_card import (
    ComputeStep,
    ConfirmedCalcPlan,
    DataFetchStep,
)

logger = logging.getLogger(__name__)


class PlanIngestionError(Exception):
    """计划摄入时发现的不可恢复错误。应中断执行并报告给用户。"""

    def __init__(self, reason: str, recoverable: bool = False) -> None:
        self.reason = reason
        self.recoverable = recoverable
        super().__init__(reason)


@dataclass(frozen=True)
class IngestedPlan:
    """摄入校验后的执行就绪计划。"""

    fetch_steps: tuple[DataFetchStep, ...]
    compute_steps: tuple[ComputeStep, ...]
    plan_summary: str
    formula: str
    output_type: str
    output_precision: str
    output_unit: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def ingest(confirmed: ConfirmedCalcPlan) -> IngestedPlan:
    """校验并摄入 ConfirmedCalcPlan。

    Raises:
        PlanIngestionError: 计划本身存在缺陷，无法执行
    """
    if not confirmed.is_locked:
        raise PlanIngestionError("计划未锁定，无法执行")

    card = confirmed.plan_card
    warnings: list[str] = []

    _validate_data_sources(confirmed, warnings)
    _validate_compute_chain(confirmed, warnings)
    _validate_output_spec(card, warnings)

    fetch_steps = tuple(confirmed.data_fetch_instructions)
    compute_steps = tuple(confirmed.compute_instructions)

    if not fetch_steps and not compute_steps:
        raise PlanIngestionError(
            "计划中既无取数步骤也无计算步骤，请重新描述需求",
            recoverable=True,
        )

    return IngestedPlan(
        fetch_steps=fetch_steps,
        compute_steps=compute_steps,
        plan_summary=confirmed.summary(),
        formula=card.formula_description,
        output_type=card.output_type,
        output_precision=card.output_precision,
        output_unit=card.output_unit,
        warnings=tuple(warnings),
    )


def _validate_data_sources(
    confirmed: ConfirmedCalcPlan,
    warnings: list[str],
) -> None:
    """校验取数指令的完整性。"""
    for step in confirmed.data_fetch_instructions:
        if not step.source.table:
            raise PlanIngestionError(
                f"取数步骤 {step.step_id} 缺少表名，计划不完整",
                recoverable=True,
            )
        if not step.source.fields:
            warnings.append(
                f"取数步骤 {step.step_id} 未指定字段列表，将使用 SELECT *"
            )


def _validate_compute_chain(
    confirmed: ConfirmedCalcPlan,
    warnings: list[str],
) -> None:
    """校验计算步骤的依赖链合法性。"""
    step_ids = {s.step_id for s in confirmed.compute_instructions}

    for step in confirmed.compute_instructions:
        if not step.description:
            raise PlanIngestionError(
                f"计算步骤 {step.step_id} 缺少描述",
                recoverable=True,
            )
        for dep in step.depends_on:
            if dep not in step_ids:
                raise PlanIngestionError(
                    f"计算步骤 {step.step_id} 依赖的步骤 {dep} 不存在",
                    recoverable=True,
                )
            if dep >= step.step_id:
                raise PlanIngestionError(
                    f"计算步骤 {step.step_id} 存在循环依赖（依赖步骤 {dep}）",
                    recoverable=True,
                )


def _validate_output_spec(card: Any, warnings: list[str]) -> None:
    """校验输出规格。"""
    if card.output_type == "未知":
        warnings.append("输出类型未指定，将尝试自动推断")
    if card.output_precision == "未知":
        warnings.append("输出精度未指定")
