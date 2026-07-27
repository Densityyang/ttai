"""评测指标计算 -- Phase 5 Benchmark 体系。

支持的指标：
- Execution Accuracy (EX): SQL 执行结果与金标一致
- Test-Suite Accuracy: 多组测试输入下执行结果与金标一致
- Dynamic Metric Success@K: CodeAct 计算结果在 K 次尝试内正确
- Value Error (MAPE/SMAPE): 数值结果与金标的偏差
- SQL Failure Rate: SQL 执行失败比率
- Code Failure Rate: 代码执行失败比率
- Auto-Repair Success Rate: 自动修复成功率
- P95 Latency: 95% 分位耗时
- Safety Interception Rate: 危险操作拦截率

并发安全：所有函数为纯函数。
"""

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CaseResult:
    """单条评测结果。"""

    case_id: str
    layer: str  # L1 / L2 / L3 / L4
    domain: str  # 业务域
    expected_mode: str  # sql_only / sql_plus_code / reject

    # 执行结果
    generated_sql: str = ""
    generated_code: str = ""
    execution_success: bool = False
    execution_error: str = ""
    output_value: Any = None
    latency_ms: float = 0.0

    # 金标比对
    gold_sql: str = ""
    gold_value: Any = None
    tolerance: float = 0.0  # 允许误差

    # 修复信息
    repair_attempts: int = 0
    repair_success: bool = False

    # 安全信息
    is_adversarial: bool = False
    was_intercepted: bool = False
    should_reject: bool = False


@dataclass
class BenchmarkReport:
    """评测报告。"""

    run_id: str
    total_cases: int = 0
    results: list[CaseResult] = field(default_factory=list)

    # 聚合指标
    execution_accuracy: float = 0.0
    dynamic_metric_success_at_1: float = 0.0
    mean_absolute_percentage_error: float = 0.0
    symmetric_mape: float = 0.0
    sql_failure_rate: float = 0.0
    code_failure_rate: float = 0.0
    auto_repair_success_rate: float = 0.0
    p95_latency_ms: float = 0.0
    safety_interception_rate: float = 0.0

    # 分层指标
    layer_accuracy: dict[str, float] = field(default_factory=dict)
    domain_accuracy: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "total_cases": self.total_cases,
            "execution_accuracy": round(self.execution_accuracy, 4),
            "dynamic_metric_success_at_1": round(self.dynamic_metric_success_at_1, 4),
            "mape": round(self.mean_absolute_percentage_error, 4),
            "smape": round(self.symmetric_mape, 4),
            "sql_failure_rate": round(self.sql_failure_rate, 4),
            "code_failure_rate": round(self.code_failure_rate, 4),
            "auto_repair_success_rate": round(self.auto_repair_success_rate, 4),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "safety_interception_rate": round(self.safety_interception_rate, 4),
            "layer_accuracy": {k: round(v, 4) for k, v in self.layer_accuracy.items()},
            "domain_accuracy": {k: round(v, 4) for k, v in self.domain_accuracy.items()},
        }


def compute_execution_accuracy(results: list[CaseResult]) -> float:
    """Execution Accuracy: 执行结果与金标一致的比率。"""
    if not results:
        return 0.0
    correct = sum(1 for r in results if _values_match(r.output_value, r.gold_value, r.tolerance))
    return correct / len(results)


def compute_dynamic_metric_success(results: list[CaseResult], k: int = 1) -> float:
    """Dynamic Metric Success@K: CodeAct 结果在 K 次内正确的比率。

    当前实现仅支持 K=1（单次执行成功即算通过）。
    """
    dynamic_cases = [r for r in results if r.expected_mode == "sql_plus_code"]
    if not dynamic_cases:
        return 0.0
    success = sum(
        1 for r in dynamic_cases
        if r.execution_success and _values_match(r.output_value, r.gold_value, r.tolerance)
    )
    return success / len(dynamic_cases)


def compute_mape(results: list[CaseResult]) -> float:
    """Mean Absolute Percentage Error (MAPE)。

    仅对有数值金标且非零的 case 计算。
    """
    errors: list[float] = []
    for r in results:
        gold = _to_numeric(r.gold_value)
        pred = _to_numeric(r.output_value)
        if gold is not None and pred is not None and gold != 0:
            errors.append(abs((gold - pred) / gold))

    return sum(errors) / len(errors) if errors else 0.0


def compute_smape(results: list[CaseResult]) -> float:
    """Symmetric MAPE: 对称百分比误差，避免分母为零问题。"""
    errors: list[float] = []
    for r in results:
        gold = _to_numeric(r.gold_value)
        pred = _to_numeric(r.output_value)
        if gold is not None and pred is not None:
            denom = abs(gold) + abs(pred)
            if denom > 0:
                errors.append(2 * abs(gold - pred) / denom)

    return sum(errors) / len(errors) if errors else 0.0


def compute_failure_rates(results: list[CaseResult]) -> tuple[float, float]:
    """(SQL 失败率, 代码失败率)。"""
    sql_cases = [r for r in results if r.expected_mode in ("sql_only", "sql_plus_code")]
    code_cases = [r for r in results if r.expected_mode == "sql_plus_code"]

    sql_fail = sum(1 for r in sql_cases if not r.execution_success and r.generated_sql) / max(len(sql_cases), 1)
    code_fail = sum(1 for r in code_cases if not r.execution_success and r.generated_code) / max(len(code_cases), 1)

    return sql_fail, code_fail


def compute_repair_success_rate(results: list[CaseResult]) -> float:
    """自动修复成功率：在有修复尝试的 case 中成功的比率。"""
    repair_cases = [r for r in results if r.repair_attempts > 0]
    if not repair_cases:
        return 0.0
    return sum(1 for r in repair_cases if r.repair_success) / len(repair_cases)


def compute_p95_latency(results: list[CaseResult]) -> float:
    """P95 延迟（毫秒）。"""
    latencies = sorted(r.latency_ms for r in results if r.latency_ms > 0)
    if not latencies:
        return 0.0
    idx = max(0, min(len(latencies) - 1, int(len(latencies) * 0.95)))
    return latencies[idx]


def compute_safety_interception_rate(results: list[CaseResult]) -> float:
    """安全拦截率：对抗样本中正确拦截的比率。"""
    adversarial = [r for r in results if r.is_adversarial or r.should_reject]
    if not adversarial:
        return 1.0
    intercepted = sum(1 for r in adversarial if r.was_intercepted)
    return intercepted / len(adversarial)


def compute_layer_accuracy(results: list[CaseResult]) -> dict[str, float]:
    """按 L1-L4 层计算准确率。"""
    layers: dict[str, list[CaseResult]] = {}
    for r in results:
        layers.setdefault(r.layer, []).append(r)
    return {
        layer: compute_execution_accuracy(cases)
        for layer, cases in sorted(layers.items())
    }


def compute_domain_accuracy(results: list[CaseResult]) -> dict[str, float]:
    """按业务域计算准确率。"""
    domains: dict[str, list[CaseResult]] = {}
    for r in results:
        domains.setdefault(r.domain, []).append(r)
    return {
        domain: compute_execution_accuracy(cases)
        for domain, cases in sorted(domains.items())
    }


def generate_report(run_id: str, results: list[CaseResult]) -> BenchmarkReport:
    """生成完整评测报告。"""
    sql_fail, code_fail = compute_failure_rates(results)

    report = BenchmarkReport(
        run_id=run_id,
        total_cases=len(results),
        results=results,
        execution_accuracy=compute_execution_accuracy(results),
        dynamic_metric_success_at_1=compute_dynamic_metric_success(results, k=1),
        mean_absolute_percentage_error=compute_mape(results),
        symmetric_mape=compute_smape(results),
        sql_failure_rate=sql_fail,
        code_failure_rate=code_fail,
        auto_repair_success_rate=compute_repair_success_rate(results),
        p95_latency_ms=compute_p95_latency(results),
        safety_interception_rate=compute_safety_interception_rate(results),
        layer_accuracy=compute_layer_accuracy(results),
        domain_accuracy=compute_domain_accuracy(results),
    )
    return report


def statistical_significance(
    baseline_scores: list[float],
    experiment_scores: list[float],
    alpha: float = 0.05,  # TUNABLE: 显著性水平
) -> dict[str, Any]:
    """配对 t 检验（简化实现，不依赖 scipy）。

    用于 A/B 对照实验的显著性检验。

    Returns:
        {"mean_diff": float, "t_statistic": float, "significant": bool, "n": int}
    """
    n = min(len(baseline_scores), len(experiment_scores))
    if n < 2:
        return {"mean_diff": 0.0, "t_statistic": 0.0, "significant": False, "n": n}

    diffs = [experiment_scores[i] - baseline_scores[i] for i in range(n)]
    mean_diff = sum(diffs) / n
    var_diff = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
    std_err = math.sqrt(var_diff / n) if var_diff > 0 else 0.0

    if std_err > 0:
        t_stat = mean_diff / std_err
    elif mean_diff:
        t_stat = math.copysign(math.inf, mean_diff)
    else:
        t_stat = 0.0

    # 简化的临界值查表（双尾 alpha=0.05）
    # 对 n>=30 使用 z=1.96, 否则使用保守估计
    # TUNABLE: 更精确的实现可引入 scipy.stats.t
    critical = 1.96 if n >= 30 else 2.0 + 5.0 / n

    return {
        "mean_diff": round(mean_diff, 6),
        "t_statistic": round(t_stat, 4),
        "significant": abs(t_stat) > critical,
        "n": n,
        "alpha": alpha,
    }


# ── 内部辅助 ──────────────────────────────────────────────────────────────────


def _values_match(output: Any, gold: Any, tolerance: float = 0.0) -> bool:
    """判断输出值与金标是否匹配。"""
    if output is None or gold is None:
        return output is None and gold is None

    out_num = _to_numeric(output)
    gold_num = _to_numeric(gold)

    if out_num is not None and gold_num is not None:
        if tolerance > 0:
            return abs(out_num - gold_num) <= tolerance
        return out_num == gold_num

    # 字符串比较（标准化后）
    return _normalize_value(str(output)) == _normalize_value(str(gold))


def _to_numeric(value: Any) -> float | None:
    """尝试转换为数值。"""
    if isinstance(value, (int, float)):
        return float(value) if not math.isnan(value) and not math.isinf(value) else None
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _normalize_value(text: str) -> str:
    """标准化值用于比较。"""
    return text.strip().lower().replace(" ", "").replace("\n", "")
