"""CodeAct 代码生成器 -- 基于 ConfirmedCalcPlan 生成可执行 Python 代码。

生成的代码严格按照用户确认的计算逻辑执行，
包含意图声明注释供验证器做一致性检查。
"""

import logging

from langchain_core.messages import SystemMessage

from src.nl2sql.agents.codeact_engine.parallel_fetcher import FetchResult
from src.nl2sql.agents.codeact_engine.plan_card import ComputeStep
from src.nl2sql.agents.codeact_engine.prompts import (
    CODE_GENERATOR_SYSTEM_PROMPT,
    CODE_REPAIR_SYSTEM_PROMPT,
)
from src.nl2sql.infra.llm.factory import get_llm

logger = logging.getLogger(__name__)


async def generate_code(
    compute_steps: tuple[ComputeStep, ...],
    formula: str,
    fetch_results: list[FetchResult],
    output_type: str,
    output_precision: str,
    output_unit: str,
) -> str | None:
    """基于确认计划的计算指令生成 Python 代码。

    Args:
        compute_steps: 用户确认的计算步骤
        formula: 计算公式描述
        fetch_results: 取数结果列表
        output_type: 输出类型
        output_precision: 精度要求
        output_unit: 单位

    Returns:
        生成的 Python 代码，或 None（生成失败）
    """
    data_vars = _build_data_variables_desc(fetch_results)
    steps_desc = _build_compute_steps_desc(compute_steps)
    sources_desc = _build_sources_desc(fetch_results)
    filters_desc = _build_filters_desc(fetch_results)

    prompt = CODE_GENERATOR_SYSTEM_PROMPT.format(
        data_sources=sources_desc,
        filters=filters_desc,
        computation_steps=steps_desc,
        formula_description=formula,
        output_type=output_type,
        output_precision=output_precision,
        output_unit=output_unit,
        data_variables=data_vars,
    )

    llm = get_llm()
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    code = _extract_code(str(response.content))

    if code:
        logger.info("CodeAct 代码生成成功 (%d 字符)", len(code))
    else:
        logger.warning("CodeAct 代码生成失败")

    return code


async def repair_code(
    code: str,
    error: str,
    fetch_results: list[FetchResult],
) -> str | None:
    """修复执行失败的代码。仅修复执行错误，不修改计算逻辑。

    Args:
        code: 原始代码
        error: 错误信息
        fetch_results: 可用数据变量描述

    Returns:
        修复后的代码，或 None
    """
    data_desc = _build_data_variables_desc(fetch_results)

    prompt = CODE_REPAIR_SYSTEM_PROMPT.format(
        code=code,
        error=error,
        data_context=data_desc or "无可用数据变量",
    )

    llm = get_llm()
    response = await llm.ainvoke([SystemMessage(content=prompt)])
    repaired = _extract_code(str(response.content))

    if repaired:
        logger.info("CodeAct 代码修复完成 (%d 字符)", len(repaired))
    else:
        logger.warning("CodeAct 代码修复失败")

    return repaired


def _build_data_variables_desc(fetch_results: list[FetchResult]) -> str:
    """构建数据变量描述。"""
    lines: list[str] = []
    for i, fr in enumerate(fetch_results):
        if not fr.success:
            lines.append(f"df_{i}: [取数失败] {fr.error}")
            continue
        preview = fr.data[:500] if fr.data else "空数据"
        lines.append(
            f"df_{i}: {fr.description}\n"
            f"  SQL: {fr.sql}\n"
            f"  行数: ~{fr.row_count}\n"
            f"  数据预览: {preview}"
        )
    return "\n".join(lines) if lines else "无可用数据变量"


def _build_compute_steps_desc(steps: tuple[ComputeStep, ...]) -> str:
    """构建计算步骤描述。"""
    lines = []
    for s in steps:
        deps = f" (依赖步骤 {s.depends_on})" if s.depends_on else ""
        lines.append(f"步骤 {s.step_id}: {s.description}{deps} -> {s.expected_output}")
    return "\n".join(lines) if lines else "无计算步骤"


def _build_sources_desc(fetch_results: list[FetchResult]) -> str:
    """构建数据来源描述。"""
    lines = [f"df_{i}: {fr.description}" for i, fr in enumerate(fetch_results) if fr.success]
    return ", ".join(lines) if lines else "无数据来源"


def _build_filters_desc(fetch_results: list[FetchResult]) -> str:
    """构建筛选条件描述。"""
    sqls = [fr.sql for fr in fetch_results if fr.success and fr.sql]
    if not sqls:
        return "无"
    return "; ".join(f"SQL_{i}: {sql[:200]}" for i, sql in enumerate(sqls))


def _extract_code(text: str) -> str | None:
    """从 LLM 输出中提取 Python 代码。"""
    content = text.strip()
    if "```python" in content:
        start = content.index("```python") + 9
        end = content.find("```", start)
        return content[start:end if end != -1 else len(content)].strip()
    if "```" in content:
        start = content.index("```") + 3
        end = content.find("```", start)
        return content[start:end if end != -1 else len(content)].strip()
    return content if content else None
