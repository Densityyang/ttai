"""评测指标计算 -- Phase 5 / P9A Benchmark 体系。

支持的指标：
- Execution Accuracy (EX): 产出值与独立 oracle 一致（分母只含可裁决 case）
- Test-Suite Accuracy: 多组测试输入下执行结果与金标一致
- Dynamic Metric Success@K: CodeAct 计算结果在 K 次尝试内正确
- Value Error (MAPE/SMAPE): 数值结果与金标的偏差
- SQL Failure Rate: SQL 执行失败比率
- Code Failure Rate: 代码执行失败比率
- Auto-Repair Success Rate: 自动修复成功率
- P95 Latency: 95% 分位耗时
- Safety Interception Rate: 危险操作拦截率

P9A 关键修正（统计分母）：
- 缺 oracle 的 case 记 UNKNOWN，既不判对也不判错，且不进入 PASS 判定；
- 分母是"可裁决 case 数"，不是 len(results)；
- 执行成功但值错 = FAIL（真正比较值，绝不用 None/None 判等冒充正确）。

并发安全：所有函数为纯函数。
"""

import math
from dataclasses import dataclass, field
from typing import Any

from benchmarks.assertions import (
    CaseObservation,
    CaseVerdict,
    adjudicate,
)
from benchmarks.assertions import (
    normalize_value as _normalize_value,
)
from benchmarks.assertions import (
    to_numeric as _to_numeric,
)
from benchmarks.assertions import (
    values_match as _values_match,
)
from benchmarks.registry import OUTCOME_TAXONOMY


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
    trace_id: str = ""
    answer_receipt: dict[str, Any] = field(default_factory=dict)
    provider_cost: float = 0.0

    # ── P9A 统一评测维度 ────────────────────────────────────────────────────
    expected_outcome: str = ""
    observed_outcome: str = ""
    adjudicated: bool = False
    passed: bool | None = None
    assertion_failures: list[str] = field(default_factory=list)
    oracle_state: str = ""
    oracle_available_override: bool | None = None
    mode: str = "QUERY"
    observed_mode: str = ""
    capability: str = "fetch"
    risk: str = "low"
    tags: list[str] = field(default_factory=list)
    expected_value_sha256: str | None = None
    output_value_sha256: str | None = None
    reference_sql_fingerprint: str | None = None
    receipt_required: bool = False
    receipt_present: bool = False
    answer_type: str = ""
    policy_outcome: str = ""
    execution_accepted: bool = False
    execution_row_count: int = 0
    candidate_score: float | None = None
    confirmed_plan_checksum: str | None = None
    observed_plan_checksum: str | None = None
    expected_row_count: int | None = None
    observed_terminal: str = ""

    def to_observation(self) -> CaseObservation:
        return CaseObservation(
            case_id=self.case_id,
            expected_outcome=self.expected_outcome,
            expected_mode=self.expected_mode,
            tags=tuple(self.tags),
            mode=self.mode,
            observed_mode=self.observed_mode,
            oracle_state=self.oracle_state,
            oracle_available_override=self.oracle_available_override,
            tolerance=self.tolerance,
            gold_value=self.gold_value,
            output_value=self.output_value,
            output_value_sha256=self.output_value_sha256,
            expected_value_sha256=self.expected_value_sha256,
            reference_sql_fingerprint=self.reference_sql_fingerprint,
            execution_success=self.execution_success,
            execution_error=self.execution_error,
            was_intercepted=self.was_intercepted,
            should_reject=self.should_reject,
            is_adversarial=self.is_adversarial,
            receipt_required=self.receipt_required,
            receipt_present=self.receipt_present,
            answer_type=self.answer_type,
            policy_outcome=self.policy_outcome,
            execution_accepted=self.execution_accepted,
            execution_row_count=self.execution_row_count,
            candidate_score=self.candidate_score,
            confirmed_plan_checksum=self.confirmed_plan_checksum,
            observed_plan_checksum=self.observed_plan_checksum,
            expected_row_count=self.expected_row_count,
            observed_terminal=self.observed_terminal,
        )


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
    manifest: dict[str, Any] = field(default_factory=dict)
    total_provider_cost: float = 0.0

    # ── P9A 分母与切片 ─────────────────────────────────────────────────────
    evidence_kind: str = "harness"
    # How many cases actually carried a structured typed receipt.  A run with
    # zero receipts proves nothing about accuracy, even when the oracle is
    # adjudicable: every such case is PROVENANCE_FAILURE, not a measured miss.
    receipts_present: int = 0
    # False when no case had an adjudicable oracle OR no case produced a typed
    # receipt: the 0.0 above is then "not established", never a measured 0%.
    accuracy_established: bool = False
    denominators: dict[str, Any] = field(default_factory=dict)
    outcome_counts: dict[str, int] = field(default_factory=dict)
    oracle_state_counts: dict[str, int] = field(default_factory=dict)
    assertion_failure_counts: dict[str, int] = field(default_factory=dict)
    mode_accuracy: dict[str, float] = field(default_factory=dict)
    capability_accuracy: dict[str, float] = field(default_factory=dict)
    risk_accuracy: dict[str, float] = field(default_factory=dict)
    mode_slices: dict[str, dict[str, Any]] = field(default_factory=dict)
    capability_slices: dict[str, dict[str, Any]] = field(default_factory=dict)
    risk_slices: dict[str, dict[str, Any]] = field(default_factory=dict)

    def for_mode(self, mode: str) -> dict[str, Any]:
        """One report can be sliced into three modes without a second run."""
        return self.mode_slices.get(mode, {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "total_cases": self.total_cases,
            "evidence_kind": self.evidence_kind,
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
            "manifest": self.manifest,
            "total_provider_cost": round(self.total_provider_cost, 6),
            "receipts_present": self.receipts_present,
            "accuracy_established": self.accuracy_established,
            "denominators": self.denominators,
            "outcome_counts": self.outcome_counts,
            "oracle_state_counts": self.oracle_state_counts,
            "assertion_failure_counts": self.assertion_failure_counts,
            "mode_accuracy": {k: round(v, 4) for k, v in self.mode_accuracy.items()},
            "capability_accuracy": {k: round(v, 4) for k, v in self.capability_accuracy.items()},
            "risk_accuracy": {k: round(v, 4) for k, v in self.risk_accuracy.items()},
            "mode_slices": self.mode_slices,
            "capability_slices": self.capability_slices,
            "risk_slices": self.risk_slices,
        }


def verdict_for(result: CaseResult) -> CaseVerdict:
    return adjudicate(result.to_observation())


def adjudicate_case_result(result: CaseResult) -> CaseResult:
    """Fill the adjudication fields of a result from its raw evidence."""
    verdict = verdict_for(result)
    result.expected_outcome = verdict.expected_outcome
    result.observed_outcome = verdict.observed_outcome
    result.adjudicated = verdict.adjudicated
    result.passed = verdict.passed
    result.assertion_failures = list(verdict.failures)
    result.oracle_state = verdict.oracle_state
    return result


def adjudicated_results(results: list[CaseResult]) -> list[tuple[CaseResult, CaseVerdict]]:
    return [(result, verdict_for(result)) for result in results]


def compute_execution_accuracy(results: list[CaseResult]) -> float:
    """Execution Accuracy: 可裁决 case 中产出值与独立 oracle 一致的比率。

    分母只包含"有独立 oracle 且可裁决"的 case。缺 oracle、只有 reference SQL
    而无具体值、以及未运行的 case 记 UNKNOWN，不进入分子也不进入分母。
    """
    if not results:
        return 0.0
    verdicts = [verdict_for(result) for result in results]
    adjudicated = [verdict for verdict in verdicts if verdict.adjudicated]
    if not adjudicated:
        return 0.0
    correct = sum(1 for verdict in adjudicated if verdict.passed)
    return correct / len(adjudicated)


def compute_dynamic_metric_success(results: list[CaseResult], k: int = 1) -> float:
    """Dynamic Metric Success@K: CodeAct 结果在 K 次内正确的比率。

    当前实现仅支持 K=1（单次执行成功且值可裁决即算通过）。
    """
    dynamic_cases = [r for r in results if r.expected_mode == "sql_plus_code"]
    if not dynamic_cases:
        return 0.0
    success = sum(
        1
        for result in dynamic_cases
        if (verdict := verdict_for(result)).adjudicated and verdict.passed
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


# ── P9A 切片与分母 ──────────────────────────────────────────────────────────


def compute_denominators(results: list[CaseResult]) -> dict[str, Any]:
    """可解释的分母：总数、可裁决、UNKNOWN 及其原因。"""
    verdicts = [verdict_for(result) for result in results]
    adjudicated = [v for v in verdicts if v.adjudicated]
    unknown = [v for v in verdicts if not v.adjudicated]
    return {
        "total_cases": len(results),
        "adjudicated_cases": len(adjudicated),
        "unknown_cases": len(unknown),
        "unknown_missing_oracle": sum(1 for v in unknown if v.oracle_state == "MISSING"),
        "unknown_reference_only": sum(1 for v in unknown if v.oracle_state == "REFERENCE_ONLY"),
        "correct": sum(1 for v in adjudicated if v.passed),
        "incorrect": sum(1 for v in adjudicated if not v.passed),
    }


def compute_outcome_counts(results: list[CaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {outcome: 0 for outcome in OUTCOME_TAXONOMY}
    for result in results:
        outcome = verdict_for(result).observed_outcome
        counts[outcome] = counts.get(outcome, 0) + 1
    return {outcome: count for outcome, count in counts.items() if count}


def compute_oracle_state_counts(results: list[CaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        state = verdict_for(result).oracle_state
        counts[state] = counts.get(state, 0) + 1
    return counts


def compute_assertion_failure_counts(results: list[CaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        for failure in result.assertion_failures:
            counts[failure] = counts.get(failure, 0) + 1
    return counts


def _slice_metrics(results: list[CaseResult]) -> dict[str, Any]:
    verdicts = [verdict_for(result) for result in results]
    adjudicated = [v for v in verdicts if v.adjudicated]
    correct = sum(1 for v in adjudicated if v.passed)
    return {
        "total_cases": len(results),
        "adjudicated_cases": len(adjudicated),
        "unknown_cases": len(verdicts) - len(adjudicated),
        "correct": correct,
        "execution_accuracy": round(correct / len(adjudicated), 4) if adjudicated else 0.0,
        "outcome_counts": _count_outcomes(verdicts),
    }


def _count_outcomes(verdicts: list[CaseVerdict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for verdict in verdicts:
        counts[verdict.observed_outcome] = counts.get(verdict.observed_outcome, 0) + 1
    return counts


def _group_slices(
    results: list[CaseResult],
    key: Any,
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[CaseResult]] = {}
    for result in results:
        groups.setdefault(str(key(result)), []).append(result)
    return {name: _slice_metrics(cases) for name, cases in sorted(groups.items())}


def compute_mode_slices(results: list[CaseResult]) -> dict[str, dict[str, Any]]:
    return _group_slices(results, lambda r: r.mode)


def compute_capability_slices(results: list[CaseResult]) -> dict[str, dict[str, Any]]:
    return _group_slices(results, lambda r: r.capability)


def compute_risk_slices(results: list[CaseResult]) -> dict[str, dict[str, Any]]:
    return _group_slices(results, lambda r: r.risk)


def _slice_accuracy(slices: dict[str, dict[str, Any]]) -> dict[str, float]:
    return {name: float(data["execution_accuracy"]) for name, data in slices.items()}


def generate_report(
    run_id: str,
    results: list[CaseResult],
    *,
    evidence_kind: str = "harness",
) -> BenchmarkReport:
    """生成完整评测报告。

    evidence_kind 默认为 "harness"：桩实现/fake provider 的结果只证明评测
    框架本身，不能冒充真实准确率。
    """
    for result in results:
        adjudicate_case_result(result)

    sql_fail, code_fail = compute_failure_rates(results)
    denominators = compute_denominators(results)
    receipts_present = sum(1 for result in results if result.receipt_present)
    denominators["receipts_present"] = receipts_present
    mode_slices = compute_mode_slices(results)
    capability_slices = compute_capability_slices(results)
    risk_slices = compute_risk_slices(results)

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
        total_provider_cost=sum(result.provider_cost for result in results),
        evidence_kind=evidence_kind,
        receipts_present=receipts_present,
        accuracy_established=denominators["adjudicated_cases"] > 0 and receipts_present > 0,
        denominators=denominators,
        outcome_counts=compute_outcome_counts(results),
        oracle_state_counts=compute_oracle_state_counts(results),
        assertion_failure_counts=compute_assertion_failure_counts(results),
        mode_accuracy=_slice_accuracy(mode_slices),
        capability_accuracy=_slice_accuracy(capability_slices),
        risk_accuracy=_slice_accuracy(risk_slices),
        mode_slices=mode_slices,
        capability_slices=capability_slices,
        risk_slices=risk_slices,
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


__all__ = [
    "BenchmarkReport",
    "CaseResult",
    "_normalize_value",
    "_to_numeric",
    "_values_match",
    "adjudicate_case_result",
    "adjudicated_results",
    "compute_assertion_failure_counts",
    "compute_capability_slices",
    "compute_denominators",
    "compute_domain_accuracy",
    "compute_dynamic_metric_success",
    "compute_execution_accuracy",
    "compute_failure_rates",
    "compute_layer_accuracy",
    "compute_mape",
    "compute_mode_slices",
    "compute_oracle_state_counts",
    "compute_outcome_counts",
    "compute_p95_latency",
    "compute_repair_success_rate",
    "compute_risk_slices",
    "compute_safety_interception_rate",
    "compute_smape",
    "generate_report",
    "statistical_significance",
    "verdict_for",
]
