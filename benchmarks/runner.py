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
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from benchmarks.adapters import (
    BenchmarkCase,
    load_bird_cases,
    load_enterprise_cases,
    load_spider_cases,
)
from benchmarks.agent_bridge import execute_case, override_agent_config
from benchmarks.metrics import (
    BenchmarkReport,
    CaseResult,
    generate_report,
    statistical_significance,
)
from benchmarks.typed_receipts import (
    BenchmarkManifest,
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
TypedExecutor = Callable[[BenchmarkCase], Awaitable[TypedAnswerReceipt]]


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
    """桩实现——不调用 agent，返回占位结果。用于纯框架测试。"""
    return CaseResult(
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
    )


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


async def run_typed_benchmark(
    cases: list[BenchmarkCase],
    *,
    manifest: BenchmarkManifest,
    executor: TypedExecutor,
    budget: BudgetGate,
) -> BenchmarkReport:
    """Run a benchmark from structured receipts, never parsed answer prose.

    The executor is deliberately injected so fake providers and a test database
    exercise the same policy, receipt and budget gates as a real benchmark.
    """
    results: list[CaseResult] = []
    for case in cases:
        receipt = await executor(case)
        validate_receipt(receipt)
        budget.consume(receipt)
        successful = receipt.execution_accepted and receipt.answer_type == "answer"
        if case.should_reject:
            successful = receipt.answer_type == "rejected" and receipt.policy_outcome == "deny"
        results.append(
            CaseResult(
                case_id=case.case_id,
                layer=case.layer,
                domain=case.domain,
                expected_mode=case.expected_mode,
                gold_sql=case.gold_sql,
                gold_value=case.gold_value,
                tolerance=case.tolerance,
                is_adversarial=case.is_adversarial,
                should_reject=case.should_reject,
                execution_success=successful,
                was_intercepted=receipt.answer_type == "rejected",
                trace_id=receipt.trace_id,
                answer_receipt=receipt.model_dump(mode="json"),
                provider_cost=sum(call.estimated_cost for call in receipt.model_calls),
            )
        )
    report = generate_report(manifest.run_id, results)
    report.manifest = manifest.model_dump(mode="json")
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
            "success": r.execution_success,
            "latency_ms": round(r.latency_ms, 1),
            "error_code": _safe_error_code(r.execution_error),
            "trace_id": r.trace_id,
        }
        for r in report.results
    ]
    json_path.write_text(json.dumps(report_dict, indent=2, ensure_ascii=False), encoding="utf-8")

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


def _redacted_sample_id(case_id: str) -> str:
    from benchmarks.typed_receipts import redacted_sample_id

    return redacted_sample_id(case_id)


def _generate_markdown_report(report: BenchmarkReport) -> str:
    """生成 Markdown 格式的评测报告。"""
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
        f"| Execution Accuracy | {report.execution_accuracy:.2%} |",
        f"| Dynamic Metric Success@1 | {report.dynamic_metric_success_at_1:.2%} |",
        f"| MAPE | {report.mean_absolute_percentage_error:.4f} |",
        f"| SMAPE | {report.symmetric_mape:.4f} |",
        f"| SQL Failure Rate | {report.sql_failure_rate:.2%} |",
        f"| Code Failure Rate | {report.code_failure_rate:.2%} |",
        f"| Auto-Repair Success Rate | {report.auto_repair_success_rate:.2%} |",
        f"| P95 Latency | {report.p95_latency_ms:.0f} ms |",
        f"| Safety Interception Rate | {report.safety_interception_rate:.2%} |",
        f"| Total Provider Cost | {report.total_provider_cost:.6f} |",
        "",
    ]

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

    # 失败案例
    failed = [r for r in report.results if not r.execution_success]
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


def main() -> None:
    """CLI 入口。"""
    parser = argparse.ArgumentParser(description="Phase 5 Benchmark Runner")
    parser.add_argument("--dataset", default="enterprise", choices=["bird", "spider", "enterprise", "all"])
    parser.add_argument("--max-cases", type=int, default=None, help="最大评测样本数")
    parser.add_argument("--experiment", default=None, help="A/B/C 实验名称")
    parser.add_argument("--concurrency", type=int, default=4, help="并发数")
    parser.add_argument("--run-id", default=None, help="自定义 run ID")
    parser.add_argument("--stub", action="store_true", help="使用桩实现（离线验证框架，不调用 agent）")
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
    else:
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
        path = save_report(report)
        print(f"\n评测报告已保存: {path}")
        print(f"EX Accuracy: {report.execution_accuracy:.2%}")
        print(f"P95 Latency: {report.p95_latency_ms:.0f}ms")


if __name__ == "__main__":
    main()
