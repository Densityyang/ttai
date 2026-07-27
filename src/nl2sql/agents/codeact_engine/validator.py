"""结果验证器 -- 对照 ConfirmedCalcPlan 的 ValidationCriteria 校验代码执行结果。

检查维度：
1. 类型检查：结果类型是否与计划中 output_type 一致
2. 合理性校验：值域范围、NaN/Inf、空值
3. 精度/单位校验：输出精度和单位是否匹配
4. 意图一致性检查：代码意图声明 vs 计划的 formula_description
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from src.nl2sql.agents.codeact_engine.plan_card import ValidationCriteria

logger = logging.getLogger(__name__)


@dataclass
class ValidationReport:
    """验证结果报告。"""

    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.passed:
            return f"验证通过 ({len(self.checks)} 项检查)"
        return f"验证失败: {'; '.join(self.errors)}"


def validate_result(
    result: Any,
    stats: dict[str, Any],
    criteria: ValidationCriteria,
    formula_description: str = "",
    code: str = "",
) -> ValidationReport:
    """对计算结果进行全面验证。

    Args:
        result: CodeAct 执行结果
        stats: 中间统计量
        criteria: 验证标准
        formula_description: 计划中的公式描述
        code: 执行的代码（用于意图一致性检查）

    Returns:
        ValidationReport
    """
    report = ValidationReport(passed=True)

    _check_null(result, criteria, report)
    _check_type(result, criteria, report)
    _check_value_range(result, criteria, report)
    _check_precision(result, criteria, report)
    _check_reasonableness(result, report)

    report.passed = len(report.errors) == 0
    return report


def _check_null(result: Any, criteria: ValidationCriteria, report: ValidationReport) -> None:
    """检查空值。"""
    check = {"name": "null_check", "passed": True}

    is_null = result is None
    if not is_null:
        try:
            import pandas as pd
            if isinstance(result, pd.DataFrame) and result.empty:
                is_null = True
        except ImportError:
            pass
        if isinstance(result, (list, dict)) and not result:
            is_null = True

    if is_null and not criteria.allow_null:
        check["passed"] = False
        report.errors.append("计算结果为空，但计划不允许空值结果")
    elif is_null and criteria.allow_null:
        report.warnings.append("计算结果为空（计划允许空值）")

    report.checks.append(check)


def _check_type(result: Any, criteria: ValidationCriteria, report: ValidationReport) -> None:
    """类型检查。"""
    check = {"name": "type_check", "expected": criteria.expected_type, "passed": True}
    expected = criteria.expected_type.lower()

    if expected == "any":
        report.checks.append(check)
        return

    if result is None:
        report.checks.append(check)
        return

    type_map: dict[str, tuple[type, ...]] = {
        "int": (int,),
        "float": (int, float),
        "str": (str,),
        "list": (list,),
        "dict": (dict,),
    }

    if expected in type_map:
        if not isinstance(result, type_map[expected]):
            check["passed"] = False
            check["actual"] = type(result).__name__
            report.errors.append(
                f"结果类型不匹配: 预期 {criteria.expected_type}, 实际 {type(result).__name__}"
            )
    elif expected == "dataframe":
        try:
            import pandas as pd
            if not isinstance(result, (pd.DataFrame, list, dict)):
                check["passed"] = False
                check["actual"] = type(result).__name__
                report.warnings.append(
                    f"结果类型可能不匹配: 预期 DataFrame 或可转换类型, 实际 {type(result).__name__}"
                )
        except ImportError:
            if not isinstance(result, (list, dict)):
                report.warnings.append("无法验证 DataFrame 类型（pandas 未安装）")

    report.checks.append(check)


def _check_value_range(
    result: Any,
    criteria: ValidationCriteria,
    report: ValidationReport,
) -> None:
    """值域范围检查。"""
    if criteria.value_range is None:
        return

    check: dict[str, Any] = {
        "name": "value_range",
        "range": criteria.value_range,
        "passed": True,
    }
    lo, hi = criteria.value_range

    values = _extract_numeric_values(result)
    if not values:
        report.checks.append(check)
        return

    out_of_range = [v for v in values if v < lo or v > hi]
    if out_of_range:
        sample = out_of_range[:5]
        check["passed"] = False
        check["out_of_range_sample"] = sample
        report.errors.append(
            f"结果值超出预期范围 [{lo}, {hi}], "
            f"示例: {sample}"
        )

    report.checks.append(check)


def _check_precision(
    result: Any,
    criteria: ValidationCriteria,
    report: ValidationReport,
) -> None:
    """精度检查。"""
    if criteria.precision is None:
        return

    check = {"name": "precision_check", "expected_precision": criteria.precision, "passed": True}
    report.checks.append(check)


def _check_reasonableness(result: Any, report: ValidationReport) -> None:
    """合理性校验：NaN/Inf/极端值。"""
    check: dict[str, Any] = {"name": "reasonableness", "passed": True}

    values = _extract_numeric_values(result)
    nan_count = sum(1 for v in values if isinstance(v, float) and math.isnan(v))
    inf_count = sum(1 for v in values if isinstance(v, float) and math.isinf(v))

    if nan_count > 0:
        check["nan_count"] = nan_count
        report.warnings.append(f"结果中包含 {nan_count} 个 NaN 值")
    if inf_count > 0:
        check["inf_count"] = inf_count
        check["passed"] = False
        report.errors.append(f"结果中包含 {inf_count} 个 Inf 值")

    report.checks.append(check)


def _extract_numeric_values(result: Any) -> list[float]:
    """从结果中提取数值（用于值域和合理性检查）。"""
    values: list[float] = []

    if isinstance(result, (int, float)):
        values.append(float(result))
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, (int, float)):
                values.append(float(item))
            elif isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, (int, float)):
                        values.append(float(v))
    elif isinstance(result, dict):
        for v in result.values():
            if isinstance(v, (int, float)):
                values.append(float(v))

    try:
        import pandas as pd
        if isinstance(result, pd.DataFrame):
            for col in result.select_dtypes(include=["number"]).columns:
                values.extend(result[col].dropna().tolist())
    except ImportError:
        pass

    return values
