"""动态指标计算 -- 计划生成器。"""

import logging

from langchain_core.messages import SystemMessage

from src.nl2sql.agents.dynamic_calc.prompts import PLANNER_SYSTEM_PROMPT
from src.nl2sql.agents.dynamic_calc.schemas import DynamicCalcPlan
from src.nl2sql.infra.llm.gateway import get_legacy_model

logger = logging.getLogger(__name__)


async def generate_calc_plan(question: str) -> DynamicCalcPlan:
    """基于用户问题生成动态计算计划。"""
    llm = get_legacy_model().with_structured_output(DynamicCalcPlan, method="function_calling")

    try:
        plan: DynamicCalcPlan = await llm.ainvoke([  # type: ignore[assignment]
            SystemMessage(content=PLANNER_SYSTEM_PROMPT),
            SystemMessage(content=f"用户问题：{question}"),
        ])
        return plan
    except Exception as e:
        logger.error("动态计算计划生成失败: %s", e)
        return DynamicCalcPlan(
            intent=question,
            data_steps=[],
            calc_steps=[],
            fallback_strategy="abort",
        )
