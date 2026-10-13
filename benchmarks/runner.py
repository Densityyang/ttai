"""Benchmark Runner -- Phase 5 评测执行引擎。

支持：
- 单数据集评测
- A/B/C 对照实验
- 结果报告生成（JSON + Markdown）
- 显著性检验

Usage:
    python -m benchmarks.runner --dataset bird --max-cases 50
    python -m benchmarks.runner --dataset enterprise --experiment ablation-crag
"""

import argparse
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmarks.adapters import (
    BenchmarkCase,
    benchmark_case_to_eval_case,
    eval_case_to_benchmark_case,
    load_bird_cases,
    load_enterprise_cases,
    load_spider_cases,
    load_synthetic_cases,
    to_eval_cases,
)
from benchmarks.agent_bridge import execute_case, override_agent_config
from benchmarks.assertions import assert_privacy_redaction
from benchmarks.executor_adapter import (
    ExecutorAdapter,
    UnavailableTypedExecutor,
    build_demo_engine_executor,
    build_unavailable_typed_executor,
    production_gate_reason,
)
from benchmarks.metrics import (
    BenchmarkReport,
    CaseResult,
    adjudicate_case_result,
    generate_report,
    statistical_significance,
)
from benchmarks.registry import (
    ASSERTION_VERSION,
    ORACLE_VERSION,
    EvalCase,
    canonical_json,
    case_set_checksum,
    dedupe_cases,
    sha256_hex,
)
from benchmarks.selection import (
    SELECTION_VERSION,
    SelectionPlan,
    SelectionRun,
    case_key,
    is_shared_regression_case,
    manifest_selection_fields,
    select_cases,
    verify_selection_checksum,
)
from benchmarks.typed_receipts import (
    BenchmarkManifest,
    BudgetExceeded,
    BudgetGate,
    TypedAnswerReceipt,
    mcnemar_exact,
    paired_bootstrap_interval,
    validate_receipt,
)

logger = logging.getLogger(__name__)

BENCHMARKS_DIR = Path(__file__).resolve().parent
DATASETS_DIR = BENCHMARKS_DIR / "datasets"
RESULTS_DIR = BENCHMARKS_DIR / "results"
# A typed run may legitimately prove NO receipt (runtime unavailable, internal
# stop reason).  Returning None is the honest fail-closed answer and the runner
# records it as PROVENANCE_FAILURE -- it is never a silent pass.
CaseInput = BenchmarkCase | EvalCase
TypedExecutor = Callable[[CaseInput], Awaitable[TypedAnswerReceipt | None]]

# The typed path always REQUIRES a receipt.  The legacy text bridge does not
# produce one, so its observations keep receipt_required=False and are judged on
# the old semantics only.
TYPED_RECEIPT_REQUIRED = True
LEGACY_RECEIPT_REQUIRED = False
DEFAULT_EXECUTOR_INVOCATIONS = 10_000


@dataclass
class ExecutorInvocationBudget:
    """Independent cap for executor invocations, including zero-call receipts."""

    max_invocations: int = DEFAULT_EXECUTOR_INVOCATIONS
    invocations_used: int = 0

    def precheck(self) -> None:
        if self.invocations_used >= self.max_invocations:
            raise BudgetExceeded("benchmark budget exhausted: executor invocation limit")

    def consume(self) -> None:
        self.invocations_used += 1


# ── A/B/C 对照实验预置配置 ────────────────────────────────────────────────────

# TUNABLE: 每个实验配置定义一组待比较的参数变化
EXPERIMENT_CONFIGS: dict[str, list[dict[str, Any]]] = {
    "ablation-crag": [
        {
            "name": "baseline-no-crag",
            "description": "禁用 CRAG 三级置信，使用原始二值评分",
            "overrides": {"crag_correct_threshold": 1.0, "crag_ambiguous_threshold": 1.0},
        },
        {
            "name": "crag-default",
            "description": "CRAG 默认阈值 (correct=0.7, ambiguous=0.4)",
            "overrides": {"crag_correct_threshold": 0.7, "crag_ambiguous_threshold": 0.4},
        },
        {
            "name": "crag-strict",
            "description": "CRAG 严格阈值 (correct=0.8, ambiguous=0.5)",
            "overrides": {"crag_correct_threshold": 0.8, "crag_ambiguous_threshold": 0.5},
        },
    ],
    "ablation-routing": [
        {
            "name": "no-adaptive-routing",
            "description": "禁用自适应路由，所有查询走 Standard Path",
            "overrides": {"enable_adaptive_routing": False},
        },
        {
            "name": "adaptive-routing",
            "description": "启用自适应路由 (Fast/Standard/Deep)",
            "overrides": {"enable_adaptive_routing": True},
        },
    ],
    "ablation-repair": [
        {
            "name": "no-experience-repair",
            "description": "禁用经验驱动修复",
            "overrides": {"enable_experience_store": False},
        },
        {
            "name": "experience-repair",
            "description": "启用经验驱动修复",
            "overrides": {"enable_experience_store": True},
        },
    ],
    "ablation-parallel-gen": [
        {
            "name": "single-strategy",
            "description": "单策略 SQL 生成",
            "overrides": {"enable_parallel_generation": False},
        },
        {
            "name": "parallel-tournament",
            "description": "多策略并行 + 锦标赛选优",
            "overrides": {"enable_parallel_generation": True},
        },
    ],
}


def load_cases(dataset: str, max_cases: int | None = None) -> list[BenchmarkCase]:
    """加载指定数据集的评测样本。"""
    if dataset == "bird":
        return load_bird_cases(DATASETS_DIR / "bird", max_cases=max_cases)
    if dataset == "spider":
        return load_spider_cases(DATASETS_DIR / "spider", max_cases=max_cases)
    if dataset == "enterprise":
        return load_enterprise_cases(DATASETS_DIR / "enterprise")
    if dataset == "p9a_typed":
        # Synthetic, explicitly P9A-owned cases spanning QUERY/ANALYZE/BUILD and
        # the demo typed runtime's bounded vocabulary.  Harness material only.
        return load_synthetic_cases(
            DATASETS_DIR / "p9a_typed", source="p9a_typed", max_cases=max_cases
        )
    if dataset == "all":
        cases: list[BenchmarkCase] = []
        cases.extend(load_bird_cases(DATASETS_DIR / "bird", max_cases=max_cases))
        cases.extend(load_spider_cases(DATASETS_DIR / "spider", max_cases=max_cases))
        cases.extend(load_enterprise_cases(DATASETS_DIR / "enterprise"))
        return cases
    raise ValueError(f"未知数据集: {dataset}")


async def run_single_case(
    case: BenchmarkCase,
    *,
    use_stub: bool = False,
) -> CaseResult:
    """执行单条评测。

    Args:
        case: 评测样本
        use_stub: True 时使用桩实现（用于离线指标测试），
                  False 时调用真实 agent bridge
    """
    if use_stub:
        return _stub_run(case)
    return await execute_case(case)


def _stub_run(case: BenchmarkCase) -> CaseResult:
    """桩实现——不调用 agent，返回占位结果。用于纯框架测试。

    桩从不产生 receipt，因此所有 case 都按"缺 receipt"裁决；这正是要证明
    的事实：桩/假 provider 只验证 harness，不产生真实准确率。
    """
    eval_case = benchmark_case_to_eval_case(case)
    result = CaseResult(
        case_id=case.case_id,
        layer=case.layer,
        domain=case.domain,
        expected_mode=case.expected_mode,
        gold_sql=case.gold_sql,
        gold_value=case.gold_value,
        tolerance=case.tolerance,
        is_adversarial=case.is_adversarial,
        should_reject=case.should_reject,
        execution_success=False,
        execution_error="stub: agent not connected",
        expected_outcome=eval_case.expected_outcome,
        oracle_state=eval_case.oracle_state,
        mode=eval_case.mode,
        capability=eval_case.capability,
        risk=eval_case.risk,
        tags=list(eval_case.tags),
        receipt_required=True,
        receipt_present=False,
    )
    return adjudicate_case_result(result)


async def run_benchmark(
    cases: list[BenchmarkCase],
    run_id: str | None = None,
    max_concurrency: int = 4,
    use_stub: bool = False,
) -> BenchmarkReport:
    """执行完整评测并生成报告。

    Args:
        use_stub: True 时使用桩实现（离线框架验证）
    """
    if not run_id:
        run_id = f"bench-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    logger.info("开始评测: run_id=%s, cases=%d, stub=%s", run_id, len(cases), use_stub)

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _bounded_run(case: BenchmarkCase) -> CaseResult:
        async with semaphore:
            return await run_single_case(case, use_stub=use_stub)

    tasks = [_bounded_run(c) for c in cases]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    case_results: list[CaseResult] = []
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            logger.warning("Case %s 执行异常: %s", cases[i].case_id, r)
            case_results.append(CaseResult(
                case_id=cases[i].case_id,
                layer=cases[i].layer,
                domain=cases[i].domain,
                expected_mode=cases[i].expected_mode,
                execution_success=False,
                execution_error=str(r),
            ))
        else:
            case_results.append(r)

    report = generate_report(run_id, case_results)
    logger.info("评测完成: EX=%.2f%%, P95=%.0fms", report.execution_accuracy * 100, report.p95_latency_ms)
    return report


def _split_case(item: CaseInput) -> tuple[BenchmarkCase, EvalCase]:
    """Normalise a run item into (executable case, adjudicated case).

    Selection hands the runner `EvalCase` objects; the legacy consumers hand it
    `BenchmarkCase`.  The evaluator must see the ORIGINAL EvalCase so a mode
    variant that selection deliberately kept is not re-derived and collapsed.
    """
    if isinstance(item, EvalCase):
        return eval_case_to_benchmark_case(item), item
    return item, benchmark_case_to_eval_case(item)


async def run_typed_benchmark(
    cases: Sequence[CaseInput],
    *,
    manifest: BenchmarkManifest,
    executor: TypedExecutor,
    budget: BudgetGate,
    evidence_kind: str | None = None,
    invocation_budget: ExecutorInvocationBudget | None = None,
    partial_on_budget_exhaustion: bool = False,
) -> BenchmarkReport:
    """Run a benchmark from structured receipts, never parsed answer prose.

    The executor is deliberately injected so fake providers and a test database
    exercise the same policy, receipt and budget gates as a real benchmark.

    A `None` receipt is a first-class outcome: the case is recorded with
    receipt_required=True and receipt_present=False, which the adjudicator turns
    into PROVENANCE_FAILURE.  A provider that simply does not answer therefore
    cannot inflate a number by being counted as a success.
    """
    results: list[CaseResult] = []
    resolved_invocation_budget = invocation_budget or ExecutorInvocationBudget()
    starting_invocations = resolved_invocation_budget.invocations_used
    budget_exhaustion_phase: str | None = None
    budget_exhaustion_reason: str | None = None
    for item in cases:
        benchmark_case, eval_case = _split_case(item)
        should_reject = (
            benchmark_case.should_reject
            or eval_case.expected_outcome == "CORRECT_REJECTION"
        )
        # Both gates run before every invocation. The independent invocation
        # cap also accounts for executors that return a receipt with no model
        # calls, which must not bypass the run budget.
        try:
            resolved_invocation_budget.precheck()
        except BudgetExceeded:
            if not partial_on_budget_exhaustion:
                raise
            budget_exhaustion_phase = "before_executor_invocation"
            budget_exhaustion_reason = "executor_invocation_limit"
            break
        try:
            budget.precheck(estimated_calls=1)
        except BudgetExceeded:
            if not partial_on_budget_exhaustion:
                raise
            budget_exhaustion_phase = "before_executor_invocation"
            budget_exhaustion_reason = "provider_budget_exhausted"
            break
        resolved_invocation_budget.consume()
        receipt = await executor(item)
        if receipt is None:
            results.append(
                adjudicate_case_result(
                    CaseResult(
                        case_id=benchmark_case.case_id,
                        layer=benchmark_case.layer,
                        domain=benchmark_case.domain,
                        expected_mode=benchmark_case.expected_mode,
                        gold_sql=benchmark_case.gold_sql,
                        gold_value=benchmark_case.gold_value,
                        tolerance=benchmark_case.tolerance,
                        is_adversarial=benchmark_case.is_adversarial,
                        should_reject=should_reject,
                        execution_success=False,
                        execution_error="typed_receipt_missing",
                        expected_outcome=eval_case.expected_outcome,
                        oracle_state=eval_case.oracle_state,
                        mode=eval_case.mode,
                        capability=eval_case.capability,
                        risk=eval_case.risk,
                        tags=list(eval_case.tags),
                        receipt_required=TYPED_RECEIPT_REQUIRED,
                        receipt_present=False,
                        expected_value_sha256=(
                            eval_case.oracle.expected_value_sha256
                            if eval_case.oracle is not None
                            else None
                        ),
                        reference_sql_fingerprint=(
                            eval_case.oracle.reference_sql_fingerprint
                            if eval_case.oracle is not None
                            else None
                        ),
                    )
                )
            )
            continue
        validate_receipt(receipt)
        receipt_cost = sum(call.estimated_cost for call in receipt.model_calls)
        receipt_calls = len(receipt.model_calls)
        budget_overrun = False
        try:
            budget.consume(receipt)
        except BudgetExceeded:
            if not partial_on_budget_exhaustion:
                raise
            # The receipt proves this invocation already happened. Preserve
            # its actual use, mark the report incomplete, and stop before any
            # further selected occurrence is invoked.
            budget.cost_used += receipt_cost
            budget.calls_used += receipt_calls
            budget_overrun = True
        successful = receipt.execution_accepted and receipt.answer_type == "answer"
        if should_reject:
            successful = receipt.answer_type == "rejected" and receipt.policy_outcome == "deny"
        result = CaseResult(
            case_id=benchmark_case.case_id,
            layer=benchmark_case.layer,
            domain=benchmark_case.domain,
            expected_mode=benchmark_case.expected_mode,
            gold_sql=benchmark_case.gold_sql,
            gold_value=benchmark_case.gold_value,
            tolerance=benchmark_case.tolerance,
            is_adversarial=benchmark_case.is_adversarial,
            should_reject=should_reject,
            execution_success=successful,
            was_intercepted=receipt.answer_type == "rejected",
            trace_id=receipt.trace_id,
            answer_receipt=receipt.model_dump(mode="json"),
            provider_cost=receipt_cost,
            expected_outcome=eval_case.expected_outcome,
            oracle_state=eval_case.oracle_state,
            mode=eval_case.mode,
            capability=eval_case.capability,
            risk=eval_case.risk,
            tags=list(eval_case.tags),
            receipt_required=TYPED_RECEIPT_REQUIRED,
            receipt_present=True,
            answer_type=receipt.answer_type,
            policy_outcome=receipt.policy_outcome,
            execution_accepted=receipt.execution_accepted,
            execution_row_count=receipt.execution_row_count,
            candidate_score=receipt.candidate_score,
            output_value=receipt.result_value,
            output_value_sha256=receipt.result_value_sha256,
            expected_value_sha256=(
                eval_case.oracle.expected_value_sha256 if eval_case.oracle is not None else None
            ),
            reference_sql_fingerprint=(
                eval_case.oracle.reference_sql_fingerprint if eval_case.oracle is not None else None
            ),
            confirmed_plan_checksum=receipt.confirmed_plan_checksum,
            observed_plan_checksum=receipt.observed_plan_checksum,
            expected_row_count=receipt.expected_row_count,
        )
        results.append(adjudicate_case_result(result))
        if budget_overrun:
            budget_exhaustion_phase = "after_executor_receipt"
            budget_exhaustion_reason = "provider_budget_exhausted"
            break
    resolved_kind = evidence_kind or getattr(executor, "evidence_kind", "harness")
    report = generate_report(manifest.run_id, results, evidence_kind=resolved_kind)
    report.manifest = manifest.model_dump(mode="json")
    if budget_exhaustion_phase is not None:
        report.execution_status = "PARTIAL_BUDGET_EXHAUSTED"
        report.execution_evidence = {
            "status": report.execution_status,
            "budget_exhaustion_phase": budget_exhaustion_phase,
            "budget_exhaustion_reason": budget_exhaustion_reason,
            "planned_occurrences": len(cases),
            "executed_occurrences": len(results),
            "unexecuted_occurrences": len(cases) - len(results),
            "executor_invocations": (
                resolved_invocation_budget.invocations_used - starting_invocations
            ),
            "executor_invocation_limit": resolved_invocation_budget.max_invocations,
            "provider_call_limit": budget.max_calls,
            "provider_call_overrun": max(0, budget.calls_used - budget.max_calls),
            "provider_cost_limit": budget.max_total_cost,
            "provider_cost_overrun": round(
                max(0.0, budget.cost_used - budget.max_total_cost), 6
            ),
        }
        report.accuracy_established = False
        report.accuracy_evidence_state = "PARTIAL_EXECUTION"
        for slices in (
            report.mode_slices,
            report.capability_slices,
            report.risk_slices,
        ):
            for data in slices.values():
                if data["adjudicated_cases"] > 0:
                    data["accuracy_established"] = False
                    data["accuracy_evidence_state"] = "PARTIAL_EXECUTION"
    return report


def save_report(report: BenchmarkReport, output_dir: Path | None = None) -> Path:
    """保存评测报告为 JSON + Markdown。"""
    out_dir = output_dir or RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # JSON 报告
    json_path = out_dir / f"{report.run_id}.json"
    report_dict = report.to_dict()
    report_dict["case_details"] = [
        {
            "sample_id": _redacted_sample_id(r.case_id),
            "layer": r.layer,
            "domain": r.domain,
            "mode": r.mode,
            "capability": r.capability,
            "risk": r.risk,
            "success": r.execution_success,
            "adjudicated": r.adjudicated,
            "passed": r.passed,
            "observed_outcome": r.observed_outcome,
            "oracle_state": r.oracle_state,
            "assertion_failures": r.assertion_failures,
            "latency_ms": round(r.latency_ms, 1),
            "error_code": _safe_error_code(r.execution_error),
            "trace_id": r.trace_id,
            **(
                {
                    "case_identity_sha256": r.case_identity_sha256,
                    "selection_checksum": r.selection_checksum,
                    "repetition_index": r.repetition_index,
                    "repetition_seed": r.repetition_seed,
                    "repetition_checksum": r.repetition_checksum,
                    "execution_run_id": r.execution_run_id,
                }
                if r.execution_run_id
                else {}
            ),
        }
        for r in report.results
    ]
    serialized = json.dumps(report_dict, indent=2, ensure_ascii=False)
    redaction = assert_privacy_redaction(
        serialized,
        forbidden=[r.case_id for r in report.results],
    )
    if not redaction.passed:
        raise RuntimeError(f"report failed privacy redaction: {redaction.failure_code}")
    json_path.write_text(serialized, encoding="utf-8")

    # Markdown 报告
    md_path = out_dir / f"{report.run_id}.md"
    md_path.write_text(_generate_markdown_report(report), encoding="utf-8")

    logger.info("报告已保存: %s, %s", json_path, md_path)
    return json_path


def _safe_error_code(error: str) -> str:
    """Reports may contain error classes but never raw model prompts/results."""
    if not error:
        return ""
    return error.split(":", 1)[0][:64]


def _accuracy_display(report: BenchmarkReport) -> str:
    if report.execution_status != "COMPLETE":
        return "not established (partial execution; budget exhausted)"
    if report.accuracy_established:
        return f"{report.execution_accuracy:.2%}"
    if report.accuracy_evidence_state == "NO_ADJUDICABLE_CASES":
        return "n/a (no adjudicable oracle)"
    if report.accuracy_evidence_state == "NO_TYPED_RECEIPTS":
        return "not established (no adjudicated typed receipts)"
    return "not established (incomplete typed receipt coverage)"


def _slice_accuracy_display(data: dict[str, Any]) -> str:
    if data.get("accuracy_established") is True:
        return f"{float(data['execution_accuracy']):.2%}"
    if data.get("accuracy_evidence_state") == "NO_ADJUDICABLE_CASES":
        return "N/A"
    return "not established"


def _redacted_sample_id(case_id: str) -> str:
    from benchmarks.typed_receipts import redacted_sample_id

    return redacted_sample_id(case_id)


def _generate_markdown_report(report: BenchmarkReport) -> str:
    """生成 Markdown 格式的评测报告。"""
    ex_display = _accuracy_display(report)
    lines: list[str] = [
        f"# Benchmark Report: {report.run_id}",
        "",
        f"**Total Cases**: {report.total_cases}",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Overall Metrics",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Execution Accuracy | {ex_display} |",
        f"| Dynamic Metric Success@1 | {report.dynamic_metric_success_at_1:.2%} |",
        f"| MAPE | {report.mean_absolute_percentage_error:.4f} |",
        f"| SMAPE | {report.symmetric_mape:.4f} |",
        f"| SQL Failure Rate | {report.sql_failure_rate:.2%} |",
        f"| Code Failure Rate | {report.code_failure_rate:.2%} |",
        f"| Auto-Repair Success Rate | {report.auto_repair_success_rate:.2%} |",
        f"| P95 Latency | {report.p95_latency_ms:.0f} ms |",
        f"| Safety Interception Rate | {report.safety_interception_rate:.2%} |",
        f"| Total Provider Cost | {report.total_provider_cost:.6f} |",
        f"| Execution state | {report.execution_status} |",
        f"| Typed receipts present | {report.receipts_present} / {report.total_cases} |",
        f"| Accuracy evidence state | {report.accuracy_evidence_state} |",
        "",
        f"**Evidence kind**: {report.evidence_kind} -- fake-provider/stub/demo runs only prove the "
        "harness, they do NOT establish real accuracy.  A run without typed receipts reports no "
        "established accuracy at all.",
        "",
        f"**Selection checksum**: {report.manifest.get('case_selection_checksum', '')}",
        f"**Selection version**: {report.manifest.get('selection_version', '')}",
        f"**Registry revision**: {report.manifest.get('registry_revision', '')}",
        "",
    ]

    execution_evidence = report.execution_evidence
    if execution_evidence:
        lines.extend(
            [
                "## Repetition Execution",
                "",
                f"- Status: {execution_evidence.get('status', report.execution_status)}",
                f"- Selection checksum: {execution_evidence.get('selection_checksum', '')}",
                f"- Requested repetitions: {execution_evidence.get('requested_repetitions', 0)}",
                f"- Completed repetitions: {execution_evidence.get('completed_repetitions', 0)}",
                f"- Executed occurrences: {execution_evidence.get('executed_occurrences', 0)} / "
                f"{execution_evidence.get('planned_occurrences', 0)}",
                f"- Executor invocations: {execution_evidence.get('executor_invocations', 0)}",
                f"- Executor invocation limit: {execution_evidence.get('executor_invocation_limit', 0)}",
                f"- Provider calls: {execution_evidence.get('provider_calls', 0)}",
                f"- Provider call limit: {execution_evidence.get('provider_call_limit', 0)}",
                f"- Provider call overrun: {execution_evidence.get('provider_call_overrun', 0)}",
                f"- Provider cost limit: {execution_evidence.get('provider_cost_limit', 0)}",
                f"- Provider cost overrun: {execution_evidence.get('provider_cost_overrun', 0)}",
                "",
                "| Repetition | Seed | Run checksum | Execution run ID | Executed / planned | Invocations | Calls | Cost | Status |",
                "|------------|------|---------------|------------------|--------------------|-------------|-------|------|--------|",
            ]
        )
        for run in execution_evidence.get("runs", []):
            lines.append(
                f"| {run.get('repetition_index', '')} | {run.get('repetition_seed', '')} "
                f"| {run.get('repetition_checksum', '')} | {run.get('execution_run_id', '')} "
                f"| {run.get('executed_occurrences', 0)} / {run.get('planned_occurrences', 0)} "
                f"| {run.get('executor_invocations', 0)} | {run.get('provider_calls', 0)} "
                f"| {run.get('provider_cost', 0.0):.6f} | {run.get('status', '')} |"
            )
        lines.append("")

    denominators = report.denominators
    if denominators:
        lines.extend([
            "## Denominators (explainable)",
            "",
            "| Bucket | Count |",
            "|--------|-------|",
            f"| Total cases | {denominators.get('total_cases', 0)} |",
            f"| Adjudicated (in EX denominator) | {denominators.get('adjudicated_cases', 0)} |",
            f"| UNKNOWN (no adjudicable oracle) | {denominators.get('unknown_cases', 0)} |",
            f"| - missing oracle | {denominators.get('unknown_missing_oracle', 0)} |",
            f"| - reference SQL only | {denominators.get('unknown_reference_only', 0)} |",
            f"| Correct | {denominators.get('correct', 0)} |",
            f"| Incorrect | {denominators.get('incorrect', 0)} |",
            "",
        ])

    if report.mode_slices:
        lines.extend([
            "## Slices by Mode",
            "",
            "| Mode | EX | Adjudicated | UNKNOWN |",
            "|------|----|-------------|---------|",
        ])
        for mode, data in sorted(report.mode_slices.items()):
            lines.append(
                f"| {mode} | {_slice_accuracy_display(data)} "
                f"| {data['adjudicated_cases']} | {data['unknown_cases']} |"
            )
        lines.append("")

    if report.capability_slices:
        lines.extend([
            "## Slices by Capability",
            "",
            "| Capability | EX | Adjudicated | UNKNOWN |",
            "|------------|----|-------------|---------|",
        ])
        for capability, data in sorted(report.capability_slices.items()):
            lines.append(
                f"| {capability} | {_slice_accuracy_display(data)} "
                f"| {data['adjudicated_cases']} | {data['unknown_cases']} |"
            )
        lines.append("")

    if report.risk_slices:
        lines.extend([
            "## Slices by Risk",
            "",
            "| Risk | EX | Adjudicated | UNKNOWN |",
            "|------|----|-------------|---------|",
        ])
        for risk, data in sorted(report.risk_slices.items()):
            lines.append(
                f"| {risk} | {_slice_accuracy_display(data)} "
                f"| {data['adjudicated_cases']} | {data['unknown_cases']} |"
            )
        lines.append("")

    if report.outcome_counts:
        lines.extend([
            "## Outcome Taxonomy Counts",
            "",
            "| Outcome | Count |",
            "|---------|-------|",
        ])
        for outcome, count in sorted(report.outcome_counts.items()):
            lines.append(f"| {outcome} | {count} |")
        lines.append("")

    if report.assertion_failure_counts:
        lines.extend([
            "## Assertion Failures",
            "",
            "| Failure code | Count |",
            "|--------------|-------|",
        ])
        for code, count in sorted(report.assertion_failure_counts.items()):
            lines.append(f"| {code} | {count} |")
        lines.append("")

    if report.layer_accuracy:
        lines.extend([
            "## Accuracy by Layer",
            "",
            "| Layer | Accuracy | Cases |",
            "|-------|----------|-------|",
        ])
        layer_counts: dict[str, int] = {}
        for r in report.results:
            layer_counts[r.layer] = layer_counts.get(r.layer, 0) + 1
        for layer, acc in sorted(report.layer_accuracy.items()):
            lines.append(f"| {layer} | {acc:.2%} | {layer_counts.get(layer, 0)} |")
        lines.append("")

    if report.domain_accuracy:
        lines.extend([
            "## Accuracy by Domain (Top 10)",
            "",
            "| Domain | Accuracy |",
            "|--------|----------|",
        ])
        sorted_domains = sorted(report.domain_accuracy.items(), key=lambda x: x[1])
        for domain, acc in sorted_domains[:10]:
            lines.append(f"| {domain} | {acc:.2%} |")
        lines.append("")

    # 失败案例（按裁决结果，而不是"执行是否成功"）
    failed = [r for r in report.results if r.passed is False]
    if failed:
        lines.extend([
            "## Failed Cases (Top 20)",
            "",
            "| Sample ID | Layer | Error code |",
            "|-----------|-------|------------|",
        ])
        for r in failed[:20]:
            error_code = _safe_error_code(r.execution_error) or "unknown"
            lines.append(f"| {_redacted_sample_id(r.case_id)} | {r.layer} | {error_code} |")
        if len(failed) > 20:
            lines.append(f"| ... | ... | {len(failed) - 20} more |")
        lines.append("")

    return "\n".join(lines)


async def run_experiment(
    experiment_name: str,
    dataset: str = "enterprise",
    max_cases: int | None = None,
    use_stub: bool = False,
) -> dict[str, BenchmarkReport]:
    """执行 A/B/C 对照实验。

    Args:
        use_stub: True 时使用桩实现（离线框架验证）

    对照实验流程:
    1. 加载统一 case 集
    2. 对每组配置：临时覆写 AgentConfig → 运行评测 → 恢复
    3. 生成对照比较报告 + 显著性检验
    """
    if experiment_name not in EXPERIMENT_CONFIGS:
        raise ValueError(
            f"未知实验: {experiment_name}. "
            f"可用: {', '.join(EXPERIMENT_CONFIGS.keys())}"
        )

    configs = EXPERIMENT_CONFIGS[experiment_name]
    cases = load_cases(dataset, max_cases=max_cases)
    if not cases:
        raise RuntimeError(f"数据集 {dataset} 为空")

    reports: dict[str, BenchmarkReport] = {}

    for config in configs:
        name = config["name"]
        overrides = config.get("overrides", {})
        logger.info("运行实验组: %s (%s) overrides=%s", name, config["description"], overrides)

        run_id = f"{experiment_name}-{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        if use_stub or not overrides:
            report = await run_benchmark(cases, run_id=run_id, use_stub=use_stub)
        else:
            with override_agent_config(overrides):
                report = await run_benchmark(cases, run_id=run_id, use_stub=use_stub)

        reports[name] = report
        save_report(report)

    _generate_comparison_report(experiment_name, reports)

    return reports


def _generate_comparison_report(
    experiment_name: str,
    reports: dict[str, BenchmarkReport],
) -> None:
    """生成 A/B/C 对照实验的比较报告。"""
    out_dir = RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        f"# Experiment Comparison: {experiment_name}",
        "",
        f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Results",
        "",
        "| Variant | EX Acc | DM S@1 | SQL Fail | P95 Lat | Safety |",
        "|---------|--------|--------|----------|---------|--------|",
    ]

    for name, report in reports.items():
        lines.append(
            f"| {name} "
            f"| {report.execution_accuracy:.2%} "
            f"| {report.dynamic_metric_success_at_1:.2%} "
            f"| {report.sql_failure_rate:.2%} "
            f"| {report.p95_latency_ms:.0f}ms "
            f"| {report.safety_interception_rate:.2%} |"
        )

    lines.append("")

    # 显著性检验（如果有 >= 2 组）
    report_list = list(reports.items())
    if len(report_list) >= 2:
        baseline_name, baseline_report = report_list[0]
        baseline_scores = [
            1.0 if r.execution_success else 0.0
            for r in baseline_report.results
        ]

        lines.extend([
            f"## Statistical Significance (vs {baseline_name})",
            "",
            "| Variant | Mean Diff | McNemar p | Paired bootstrap 95% CI |",
            "|---------|-----------|-----------|-------------------------|",
        ])

        for name, report in report_list[1:]:
            exp_scores = [
                1.0 if r.execution_success else 0.0
                for r in report.results
            ]
            sig = statistical_significance(baseline_scores, exp_scores)
            mcnemar = mcnemar_exact(
                [bool(score) for score in baseline_scores],
                [bool(score) for score in exp_scores],
            )
            low, high = paired_bootstrap_interval(baseline_scores, exp_scores)
            lines.append(
                f"| {name} "
                f"| {sig['mean_diff']:+.4f} "
                f"| {mcnemar['p_value']:.4f} "
                f"| [{low:+.4f}, {high:+.4f}] |"
            )

        lines.append("")

    md_path = out_dir / f"comparison-{experiment_name}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("对照报告已生成: %s", md_path)


# ── P9A typed default path ──────────────────────────────────────────────────

# A benchmark process holds no trusted Backend AuthorizationContext, so the
# production typed runtime fails closed on its first gate.  These budgets bound
# a fake-provider harness run; they are not a production allowance.
DEFAULT_BUDGET_COST = 25.0
DEFAULT_BUDGET_CALLS = 10_000


def _git_revision() -> str:
    """Best-effort read-only revision stamp; never a git write."""
    import os
    import subprocess

    from_env = os.environ.get("TTAI_GIT_REVISION", "").strip()
    if len(from_env) >= 7:
        return from_env
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        revision = completed.stdout.strip()
        if len(revision) >= 7:
            return revision
    except Exception:  # noqa: BLE001 - a missing git is not a run failure
        pass
    return "0000000"


def build_manifest(
    *,
    run_id: str,
    plan: SelectionPlan,
    dataset: str,
) -> BenchmarkManifest:
    """Turn a selection plan into the run manifest, checksum included."""
    return BenchmarkManifest(
        run_id=run_id,
        dataset_checksum=plan.dataset_checksum,
        prompt_version="p9a-typed-v1",
        policy_version="p9a-typed-v1",
        semantic_version="p9a-typed-v1",
        model_profile_version="p9a-typed-v1",
        git_revision=_git_revision(),
        matrix=(),
        enabled_modes=tuple(sorted(plan.mode_counts())),
        **manifest_selection_fields(plan),
        registry_revision="p9a-registry-v1",
        oracle_version=ORACLE_VERSION,
        assertion_version=ASSERTION_VERSION,
        data_reference=dataset,
        replay_reference=f"selection:{plan.checksum[:16]}",
        legacy_extras={
            "selection_reason": plan.reason,
            "selection_repetitions": plan.repetitions,
            "selection_conservative": plan.conservative,
            "selection_max_cases": plan.max_cases,
            "selection_sampling_skipped": plan.sampling_skipped,
        },
    )


def _expected_selection_run_seed(plan: SelectionPlan, index: int) -> int:
    digest = sha256_hex(f"{plan.selection_version}:{plan.seed}:{index}")
    return int(digest[:8], 16)


def _expected_selection_run_checksum(
    plan: SelectionPlan, run: SelectionRun
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "selection_version": plan.selection_version,
                "index": run.index,
                "seed": run.seed,
                "case_keys": list(run.case_keys),
            }
        )
    )


def _case_identity_sha256(case: EvalCase) -> str:
    return sha256_hex(
        canonical_json(
            {"case_id": case.case_id, "revision": case.revision, "mode": case.mode}
        )
    )


def _validate_eval_case_identities(cases: Sequence[EvalCase]) -> None:
    revisions_by_key: dict[str, str] = {}
    for case in cases:
        key = case_key(case)
        prior_revision = revisions_by_key.get(key)
        if prior_revision is not None and prior_revision != case.revision:
            raise ValueError(
                "dataset has conflicting revisions for one case/mode selection key"
            )
        revisions_by_key[key] = case.revision


def _execution_run_id(
    manifest_run_id: str, plan: SelectionPlan, run: SelectionRun
) -> str:
    return "rep-" + sha256_hex(
        canonical_json(
            {
                "manifest_run_id": manifest_run_id,
                "selection_checksum": plan.checksum,
                "repetition_index": run.index,
                "repetition_seed": run.seed,
                "repetition_checksum": run.checksum,
            }
        )
    )[:24]


def _validate_selection_plan_for_execution(
    plan: SelectionPlan,
    *,
    source_cases: Sequence[EvalCase],
) -> dict[str, EvalCase]:
    if not verify_selection_checksum(plan):
        raise ValueError("selection plan checksum mismatch")
    if plan.selection_version != SELECTION_VERSION:
        raise ValueError("unsupported selection plan version")
    if plan.repetitions != len(plan.runs):
        raise ValueError("selection repetition count does not match its run list")

    reviewed_source = dedupe_cases(source_cases)
    if plan.dataset_checksum != case_set_checksum(reviewed_source):
        raise ValueError("selection plan dataset checksum does not match its source cases")
    source_by_key = {case_key(case): case for case in reviewed_source}
    mandatory_shared_keys = tuple(
        case_key(case) for case in reviewed_source if is_shared_regression_case(case)
    )
    if plan.shared_regression_keys != mandatory_shared_keys:
        raise ValueError("selection plan shared regression keys do not match source cases")

    cases_by_key: dict[str, EvalCase] = {}
    for case in plan.cases:
        key = case_key(case)
        if key in cases_by_key:
            raise ValueError("selection plan has duplicate case/mode identity")
        source_case = source_by_key.get(key)
        if source_case is None or source_case.revision != case.revision:
            raise ValueError("selection plan case identity/revision differs from source cases")
        cases_by_key[key] = case

    planned_case_keys = plan.case_keys()
    if not set(mandatory_shared_keys) <= set(planned_case_keys):
        raise ValueError("selection plan omits mandatory shared regression coverage")
    if plan.conservative and set(planned_case_keys) != set(source_by_key):
        raise ValueError("conservative selection plan must retain full source coverage")

    for expected_index, run in enumerate(plan.runs):
        if run.index != expected_index:
            raise ValueError("selection run indexes are not contiguous")
        if run.seed != _expected_selection_run_seed(plan, expected_index):
            raise ValueError("selection run seed does not match the verified plan")
        if run.checksum != _expected_selection_run_checksum(plan, run):
            raise ValueError("selection run checksum mismatch")
        if len(run.case_keys) != len(set(run.case_keys)):
            raise ValueError("selection run repeats a case/mode occurrence")
        if not set(run.case_keys) <= cases_by_key.keys():
            raise ValueError("selection run refers to a case outside the verified plan")
        if not set(mandatory_shared_keys) <= set(run.case_keys):
            raise ValueError("selection run omits mandatory shared regression coverage")
        if run.case_keys != planned_case_keys:
            raise ValueError("selection run omits or reorders a selected occurrence")
    return cases_by_key


async def _execute_typed_selection_plan(
    plan: SelectionPlan,
    *,
    source_cases: Sequence[EvalCase],
    manifest: BenchmarkManifest,
    executor: TypedExecutor,
    budget: BudgetGate,
    max_executor_invocations: int = DEFAULT_EXECUTOR_INVOCATIONS,
) -> BenchmarkReport:
    """Execute exactly the verified occurrences named by each SelectionRun."""
    cases_by_key = _validate_selection_plan_for_execution(
        plan, source_cases=source_cases
    )
    invocation_budget = ExecutorInvocationBudget(max_executor_invocations)
    all_results: list[CaseResult] = []
    run_evidence: list[dict[str, Any]] = []
    budget_exhausted = False
    planned_occurrences = sum(len(run.case_keys) for run in plan.runs)

    for run_position, selected_run in enumerate(plan.runs):
        run_cases = [cases_by_key[key] for key in selected_run.case_keys]
        execution_run_id = _execution_run_id(manifest.run_id, plan, selected_run)
        run_manifest = manifest.model_copy(update={"run_id": execution_run_id})

        if budget_exhausted:
            run_evidence.append(
                {
                    "status": "NOT_STARTED_BUDGET_EXHAUSTED",
                    "repetition_index": selected_run.index,
                    "repetition_seed": selected_run.seed,
                    "repetition_checksum": selected_run.checksum,
                    "execution_run_id": execution_run_id,
                    "planned_occurrences": len(run_cases),
                    "executed_occurrences": 0,
                    "unexecuted_occurrences": len(run_cases),
                    "executor_invocations": 0,
                    "provider_calls": 0,
                    "provider_cost": 0.0,
                    "unexecuted_occurrence_sha256": [
                        _case_identity_sha256(case) for case in run_cases
                    ],
                }
            )
            continue

        invocations_before = invocation_budget.invocations_used
        provider_calls_before = budget.calls_used
        provider_cost_before = budget.cost_used
        run_report = await run_typed_benchmark(
            run_cases,
            manifest=run_manifest,
            executor=executor,
            budget=budget,
            invocation_budget=invocation_budget,
            partial_on_budget_exhaustion=True,
        )
        completed_cases = run_cases[: len(run_report.results)]
        for case, result in zip(completed_cases, run_report.results, strict=True):
            result.case_identity_sha256 = _case_identity_sha256(case)
            result.selection_checksum = plan.checksum
            result.repetition_index = selected_run.index
            result.repetition_seed = selected_run.seed
            result.repetition_checksum = selected_run.checksum
            result.execution_run_id = execution_run_id

        unexecuted_cases = run_cases[len(run_report.results) :]
        run_status = run_report.execution_status
        run_evidence.append(
            {
                "status": run_status,
                "repetition_index": selected_run.index,
                "repetition_seed": selected_run.seed,
                "repetition_checksum": selected_run.checksum,
                "execution_run_id": execution_run_id,
                "planned_occurrences": len(run_cases),
                "executed_occurrences": len(run_report.results),
                "unexecuted_occurrences": len(unexecuted_cases),
                "executor_invocations": (
                    invocation_budget.invocations_used - invocations_before
                ),
                "provider_calls": budget.calls_used - provider_calls_before,
                "provider_cost": round(budget.cost_used - provider_cost_before, 6),
                "provider_call_overrun": max(0, budget.calls_used - budget.max_calls),
                "provider_cost_overrun": round(
                    max(0.0, budget.cost_used - budget.max_total_cost), 6
                ),
                "unexecuted_occurrence_sha256": [
                    _case_identity_sha256(case) for case in unexecuted_cases
                ],
                "budget_exhaustion_phase": run_report.execution_evidence.get(
                    "budget_exhaustion_phase"
                ),
                "budget_exhaustion_reason": run_report.execution_evidence.get(
                    "budget_exhaustion_reason"
                ),
            }
        )
        all_results.extend(run_report.results)
        if run_status != "COMPLETE":
            budget_exhausted = True

    report = generate_report(
        manifest.run_id,
        all_results,
        evidence_kind=getattr(executor, "evidence_kind", "harness"),
    )
    report.manifest = manifest.model_dump(mode="json")
    report.execution_status = (
        "PARTIAL_BUDGET_EXHAUSTED" if budget_exhausted else "COMPLETE"
    )
    report.execution_evidence = {
        "status": report.execution_status,
        "selection_checksum": plan.checksum,
        "selection_version": plan.selection_version,
        "requested_repetitions": plan.repetitions,
        "completed_repetitions": sum(
            1 for run in run_evidence if run["status"] == "COMPLETE"
        ),
        "planned_occurrences": planned_occurrences,
        "executed_occurrences": len(all_results),
        "unexecuted_occurrences": planned_occurrences - len(all_results),
        "executor_invocations": invocation_budget.invocations_used,
        "executor_invocation_limit": invocation_budget.max_invocations,
        "provider_calls": budget.calls_used,
        "provider_call_limit": budget.max_calls,
        "provider_call_overrun": max(0, budget.calls_used - budget.max_calls),
        "provider_cost": round(budget.cost_used, 6),
        "provider_cost_limit": budget.max_total_cost,
        "provider_cost_overrun": round(
            max(0.0, budget.cost_used - budget.max_total_cost), 6
        ),
        "runs": run_evidence,
    }
    if budget_exhausted:
        report.accuracy_established = False
        report.accuracy_evidence_state = "PARTIAL_EXECUTION"
        for slices in (
            report.mode_slices,
            report.capability_slices,
            report.risk_slices,
        ):
            for data in slices.values():
                if data["adjudicated_cases"] > 0:
                    data["accuracy_established"] = False
                    data["accuracy_evidence_state"] = "PARTIAL_EXECUTION"
    return report


def build_typed_executor(
    typed_runtime: str,
) -> ExecutorAdapter | UnavailableTypedExecutor:
    """Resolve the typed executor for one CLI/API run.

    - production: the trusted Backend authority a benchmark process does not
      have, so this fails closed with authorization_context_missing and every
      case is PROVENANCE_FAILURE.
    - demo: the real engine over the demo fixture runtime (harness evidence).
    - none: nothing runs; still no receipt, never a silent pass.
    """
    if typed_runtime == "demo":
        return build_demo_engine_executor()
    if typed_runtime == "none":
        return build_unavailable_typed_executor(reason="typed_runtime_not_requested")
    # Ask the REAL production gate why it refuses, instead of asserting a reason.
    return build_unavailable_typed_executor(reason=production_gate_reason())


def run_typed_dataset(
    *,
    dataset: str,
    changes: Sequence[str] = (),
    seed: int = 0,
    repetitions: int = 1,
    max_cases: int | None = None,
    typed_runtime: str = "production",
    run_id: str | None = None,
    max_executor_invocations: int = DEFAULT_EXECUTOR_INVOCATIONS,
) -> tuple[BenchmarkReport, SelectionPlan]:
    """Load, select, and run one typed benchmark.  Used by the CLI and tests."""
    benchmark_cases = load_cases(dataset, max_cases=None)
    if not benchmark_cases:
        raise RuntimeError(f"数据集 {dataset} 为空")
    eval_cases = to_eval_cases(benchmark_cases)
    _validate_eval_case_identities(eval_cases)
    plan = select_cases(
        eval_cases,
        changes=changes,
        seed=seed,
        repetitions=repetitions,
        max_cases=max_cases,
    )
    _validate_selection_plan_for_execution(plan, source_cases=eval_cases)
    executor = build_typed_executor(typed_runtime)
    resolved_run_id = run_id or f"p9a-typed-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    manifest = build_manifest(run_id=resolved_run_id, plan=plan, dataset=dataset)
    report = asyncio.run(
        _execute_typed_selection_plan(
            plan,
            source_cases=eval_cases,
            manifest=manifest,
            executor=executor,
            budget=BudgetGate(
                max_total_cost=DEFAULT_BUDGET_COST,
                max_calls=DEFAULT_BUDGET_CALLS,
            ),
            max_executor_invocations=max_executor_invocations,
        )
    )
    return report, plan


def main() -> None:
    """CLI 入口。默认走 typed 路径；legacy 文本 bridge 需要显式开关。"""
    parser = argparse.ArgumentParser(description="Phase 5 Benchmark Runner")
    parser.add_argument(
        "--dataset",
        default="enterprise",
        choices=["bird", "spider", "enterprise", "p9a_typed", "all"],
    )
    parser.add_argument("--max-cases", type=int, default=None, help="最大评测样本数")
    parser.add_argument("--experiment", default=None, help="A/B/C 实验名称")
    parser.add_argument("--concurrency", type=int, default=4, help="并发数")
    parser.add_argument("--run-id", default=None, help="自定义 run ID")
    parser.add_argument("--output-dir", default=None, help="报告输出目录")
    parser.add_argument("--stub", action="store_true", help="使用桩实现（离线验证框架，不调用 agent）")
    parser.add_argument(
        "--legacy-bridge",
        action="store_true",
        help="使用 legacy 文本 bridge（正则抽数/关键词判拒答，不要求 receipt）",
    )
    parser.add_argument(
        "--typed-runtime",
        default="production",
        choices=["production", "demo", "none"],
        help="typed 路径的运行时：production 无 Backend 授权即 fail-closed；demo 为 harness 证据",
    )
    parser.add_argument(
        "--change",
        action="append",
        default=None,
        help="变更的影响面（子模块或 capability，可重复）；未知变更扩张为全量回归",
    )
    parser.add_argument("--seed", type=int, default=0, help="selection 随机种子")
    parser.add_argument("--repetitions", type=int, default=1, help="selection 重复次数")
    parser.add_argument(
        "--selection-only", action="store_true", help="只打印 selection，不执行"
    )
    parser.add_argument(
        "--list-experiments", action="store_true",
        help="列出可用的 A/B/C 对照实验配置",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.list_experiments:
        print("可用实验配置:")
        for name, configs in EXPERIMENT_CONFIGS.items():
            print(f"\n  {name}:")
            for c in configs:
                print(f"    - {c['name']}: {c['description']}")
        return

    if args.experiment:
        reports = asyncio.run(run_experiment(
            experiment_name=args.experiment,
            dataset=args.dataset,
            max_cases=args.max_cases,
            use_stub=args.stub,
        ))
        for name, report in reports.items():
            print(f"\n{name}: EX={report.execution_accuracy:.2%}, P95={report.p95_latency_ms:.0f}ms")
        return

    if args.legacy_bridge:
        # EXPLICIT opt-out of the typed contract.  This path keeps its legacy
        # text semantics and receipt_required=False; it proves nothing about
        # provenance and must never be the default.
        cases = load_cases(args.dataset, max_cases=args.max_cases)
        if not cases:
            print(f"数据集 {args.dataset} 为空，请先下载数据")
            return
        report = asyncio.run(run_benchmark(
            cases,
            run_id=args.run_id,
            max_concurrency=args.concurrency,
            use_stub=args.stub,
        ))
        report.evidence_kind = "legacy-text"
        path = save_report(report, Path(args.output_dir) if args.output_dir else None)
        print("\n[路径] legacy text bridge (receipt_required=False)")
        print(f"评测报告已保存: {path}")
        print(f"EX Accuracy: {report.execution_accuracy:.2%}")
        print(f"Evidence kind: {report.evidence_kind}")
        return

    if args.stub:
        cases = load_cases(args.dataset, max_cases=args.max_cases)
        if not cases:
            print(f"数据集 {args.dataset} 为空，请先下载数据")
            return
        report = asyncio.run(run_benchmark(cases, run_id=args.run_id, use_stub=True))
        path = save_report(report, Path(args.output_dir) if args.output_dir else None)
        print("\n[路径] stub (无 receipt，harness 证据)")
        print(f"评测报告已保存: {path}")
        print(f"EX Accuracy: {report.execution_accuracy:.2%}")
        print(f"Evidence kind: {report.evidence_kind}")
        return

    # ── default: typed path ────────────────────────────────────────────────
    benchmark_cases = load_cases(args.dataset, max_cases=None)
    if not benchmark_cases:
        print(f"数据集 {args.dataset} 为空，请先下载数据")
        return
    eval_cases = to_eval_cases(benchmark_cases)
    _validate_eval_case_identities(eval_cases)
    plan = select_cases(
        eval_cases,
        changes=args.change or [],
        seed=args.seed,
        repetitions=args.repetitions,
        max_cases=args.max_cases,
    )
    _validate_selection_plan_for_execution(plan, source_cases=eval_cases)
    print("[路径] typed")
    print(
        f"selection: reason={plan.reason} checksum={plan.checksum} "
        f"cases={len(plan.cases)} modes={json.dumps(plan.mode_counts(), sort_keys=True)} "
        f"seed={plan.seed} repetitions={plan.repetitions} "
        f"sampling_skipped={plan.sampling_skipped}"
    )
    if plan.unknown_changes:
        print(f"selection: unknown changes widened the run: {list(plan.unknown_changes)}")
    if args.selection_only:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return

    executor = build_typed_executor(args.typed_runtime)
    if isinstance(executor, UnavailableTypedExecutor):
        print(f"typed-runtime unavailable reason: {executor.reason}")
    resolved_run_id = args.run_id or f"p9a-typed-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    manifest = build_manifest(run_id=resolved_run_id, plan=plan, dataset=args.dataset)
    report = asyncio.run(
        _execute_typed_selection_plan(
            plan,
            source_cases=eval_cases,
            manifest=manifest,
            executor=executor,
            budget=BudgetGate(
                max_total_cost=DEFAULT_BUDGET_COST,
                max_calls=DEFAULT_BUDGET_CALLS,
            ),
        )
    )
    path = save_report(report, Path(args.output_dir) if args.output_dir else None)
    print(f"typed-runtime: {args.typed_runtime}")
    print(f"评测报告已保存: {path}")
    print(f"Evidence kind: {report.evidence_kind}")
    print(f"execution_status: {report.execution_status}")
    if report.execution_evidence:
        print(
            "repetition occurrences: "
            f"{report.execution_evidence.get('executed_occurrences', 0)}/"
            f"{report.execution_evidence.get('planned_occurrences', 0)}"
        )
    print(f"accuracy_established: {report.accuracy_established}")
    print(f"accuracy_evidence_state: {report.accuracy_evidence_state}")
    print(f"receipts_present: {report.receipts_present}/{report.total_cases}")
    print(f"EX Accuracy: {_accuracy_display(report)}")
    print(
        "mode slices: "
        + json.dumps(sorted(report.mode_slices), ensure_ascii=False)
    )
    print(f"manifest case_selection_checksum: {report.manifest.get('case_selection_checksum')}")


if __name__ == "__main__":
    main()
