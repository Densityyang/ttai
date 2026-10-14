from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from benchmarks.adapters import BenchmarkCase
from benchmarks.comparison import compare_reports
from benchmarks.metrics import (
    BenchmarkReport,
    CaseResult,
    statistical_significance,
)
from benchmarks.registry import EvalCase
from benchmarks.runner import _generate_comparison_report
from benchmarks.typed_receipts import (
    ProviderCallReceipt,
    TypedAnswerReceipt,
    mcnemar_exact,
    paired_bootstrap_interval,
)

_DATASET = "a" * 64
_SELECTION = "b" * 64


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _TypedHarnessExecutor:
    evidence_kind = "harness"

    def __init__(self, model: str) -> None:
        self.model = model

    async def __call__(self, case: EvalCase) -> TypedAnswerReceipt:
        expected = case.oracle.expected_value if case.oracle is not None else 0
        result = (
            -1
            if self.model == "candidate-model" and case.case_id == "case-a"
            else expected
        )
        return TypedAnswerReceipt(
            trace_id="synthetic-harness-trace",
            answer_type="answer",
            answer_hash="a" * 64,
            candidate_score=1.0,
            policy_outcome="allow",
            execution_accepted=True,
            execution_row_count=1,
            result_value=result,
        )


class _OverBudgetTypedExecutor:
    evidence_kind = "harness"

    async def __call__(self, case: EvalCase) -> TypedAnswerReceipt:
        value = case.oracle.expected_value if case.oracle is not None else 0
        return TypedAnswerReceipt(
            trace_id="synthetic-over-budget-trace",
            answer_type="answer",
            answer_hash="b" * 64,
            candidate_score=1.0,
            policy_outcome="allow",
            execution_accepted=True,
            execution_row_count=1,
            result_value=value,
            model_calls=(
                ProviderCallReceipt(
                    alias="fast.default",
                    stage="answer",
                    resolved_model="synthetic-model",
                    input_tokens=1,
                    output_tokens=1,
                    estimated_cost=100.0,
                    latency_ms=1.0,
                ),
            ),
        )


def _report(
    run_id: str,
    outcomes: dict[tuple[str, int], bool | None],
    *,
    order: list[tuple[str, int]] | None = None,
    dataset_checksum: str = _DATASET,
    selection_checksum: str = _SELECTION,
    revision: str | None = None,
    execution_status: str = "COMPLETE",
    accuracy_established: bool = True,
) -> BenchmarkReport:
    identities = order or list(outcomes)
    runs: dict[int, dict[str, Any]] = {}
    results: list[CaseResult] = []
    for case_id, repetition_index in identities:
        seed = 100 + repetition_index
        repetition_checksum = _sha(f"repetition:{repetition_index}")
        execution_run_id = f"{run_id}-repetition-{repetition_index}"
        run = runs.setdefault(
            repetition_index,
            {
                "status": "COMPLETE",
                "repetition_index": repetition_index,
                "repetition_seed": seed,
                "repetition_checksum": repetition_checksum,
                "execution_run_id": execution_run_id,
                "planned_occurrences": 0,
                "executed_occurrences": 0,
                "unexecuted_occurrences": 0,
            },
        )
        run["planned_occurrences"] = int(run["planned_occurrences"]) + 1
        run["executed_occurrences"] = int(run["executed_occurrences"]) + 1
        outcome = outcomes[(case_id, repetition_index)]
        results.append(
            CaseResult(
                case_id=case_id,
                layer="L1",
                domain="synthetic",
                expected_mode="sql_only",
                mode="QUERY",
                case_identity_sha256=_sha(f"case:{case_id}:revision-1:QUERY"),
                selection_checksum=selection_checksum,
                repetition_index=repetition_index,
                repetition_seed=seed,
                repetition_checksum=repetition_checksum,
                execution_run_id=execution_run_id,
                adjudicated=outcome is not None,
                passed=outcome,
            )
        )
    planned = len(results)
    manifest = {
        "run_id": run_id,
        "dataset_checksum": dataset_checksum,
        "git_revision": revision or run_id + "-revision",
        "case_selection_checksum": selection_checksum,
        "selection_version": "selection-v1",
        "seed": 42,
        "registry_revision": "registry-v1",
        "oracle_version": "oracle-v1",
        "assertion_version": "assertion-v1",
        "prompt_version": "prompt-v1",
        "policy_version": "policy-v1",
        "semantic_version": "semantic-v1",
        "model_profile_version": run_id + "-model",
        "matrix": [run_id + "-provider"],
        "enabled_modes": ["QUERY"],
    }
    return BenchmarkReport(
        run_id=run_id,
        total_cases=planned,
        results=results,
        manifest=manifest,
        execution_status=execution_status,
        accuracy_established=accuracy_established,
        accuracy_evidence_state=(
            "MEASURED_TYPED" if accuracy_established else "NO_TYPED_RECEIPTS"
        ),
        execution_evidence={
            "status": execution_status,
            "selection_checksum": selection_checksum,
            "selection_version": "selection-v1",
            "planned_occurrences": planned,
            "executed_occurrences": planned,
            "unexecuted_occurrences": 0,
            "requested_repetitions": len(runs),
            "completed_repetitions": len(runs),
            "runs": [runs[index] for index in sorted(runs)],
        },
    )


def test_keyed_comparison_pairs_reordered_cases_and_repetitions() -> None:
    identities = [("case-a", 0), ("case-b", 0), ("case-a", 1), ("case-b", 1)]
    baseline = _report(
        "baseline",
        {
            ("case-a", 0): True,
            ("case-b", 0): False,
            ("case-a", 1): False,
            ("case-b", 1): True,
        },
        order=identities,
        revision="base-1234567",
    )
    candidate = _report(
        "candidate",
        {
            ("case-a", 0): False,
            ("case-b", 0): True,
            ("case-a", 1): False,
            ("case-b", 1): True,
        },
        order=list(reversed(identities)),
        revision="candidate-7654321",
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison.status == "PAIRED_DESCRIPTIVE"
    assert comparison.pair_count == 4
    assert comparison.adjudicated_pair_count == 4
    assert comparison.mean_paired_pass_delta == 0.0
    assert comparison.outcome_counts == {
        "baseline_only_pass": 1,
        "both_pass": 1,
        "both_fail": 1,
        "candidate_only_pass": 1,
    }
    assert comparison.manifest["baseline"]["git_revision"] == "base-1234567"
    assert comparison.manifest["candidate"]["git_revision"] == "candidate-7654321"
    assert comparison.manifest["baseline"]["runtime_config_sha256"] != comparison.manifest[
        "candidate"
    ]["runtime_config_sha256"]
    assert "case-a" not in json.dumps(comparison.manifest)
    assert "pairs" not in comparison.manifest
    assert len(comparison.manifest["pair_identity_set_sha256"]) == 64


def test_keyed_comparison_keeps_unknown_pairs_out_of_descriptive_denominator() -> None:
    identities = [("known", 0), ("unknown", 0)]
    baseline = _report(
        "baseline",
        {("known", 0): False, ("unknown", 0): None},
        order=identities,
    )
    candidate = _report(
        "candidate",
        {("known", 0): True, ("unknown", 0): False},
        order=list(reversed(identities)),
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison.status == "PAIRED_DESCRIPTIVE"
    assert comparison.pair_count == 2
    assert comparison.adjudicated_pair_count == 1
    assert comparison.unknown_pair_count == 1
    assert comparison.mean_paired_pass_delta == 1.0


def test_keyed_comparison_does_not_promote_unestablished_accuracy() -> None:
    baseline = _report(
        "baseline", {("case-a", 0): True}, accuracy_established=False
    )
    candidate = _report(
        "candidate", {("case-a", 0): True}, accuracy_established=False
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison.status == "PAIRED_DESCRIPTIVE"
    assert comparison.adjudicated_pair_count == 0
    assert comparison.unknown_pair_count == 1
    assert comparison.mean_paired_pass_delta is None


def test_keyed_comparison_refuses_missing_extra_and_duplicate_pairs() -> None:
    baseline = _report(
        "baseline",
        {("case-a", 0): True, ("case-b", 0): False},
    )
    missing = _report("candidate", {("case-a", 0): True})
    duplicate = _report(
        "duplicate",
        {("case-a", 0): True, ("case-b", 0): False},
        order=[("case-a", 0), ("case-a", 0)],
    )

    missing_result = compare_reports(baseline, missing)
    duplicate_result = compare_reports(baseline, duplicate)

    assert missing_result.status == "REFUSED"
    assert missing_result.reason == "pair_set_mismatch_missing_1_extra_0"
    assert duplicate_result.status == "REFUSED"
    assert duplicate_result.reason == "duplicate_pair_identity"


def test_keyed_comparison_refuses_revision_mode_selection_and_partial_mismatches() -> None:
    baseline = _report("baseline", {("case-a", 0): True})
    dataset_mismatch = _report(
        "dataset", {("case-a", 0): True}, dataset_checksum="d" * 64
    )
    revision_mismatch = _report(
        "candidate", {("case-a", 0): True}, selection_checksum="c" * 64
    )
    partial = _report(
        "partial", {("case-a", 0): True}, execution_status="PARTIAL_BUDGET_EXHAUSTED"
    )
    mode_mismatch = _report("mode", {("case-a", 0): True})
    mode_mismatch.results[0].mode = "ANALYZE"
    repetition_mismatch = _report("repetition", {("case-a", 1): True})
    case_revision_mismatch = _report("case-revision", {("case-a", 0): True})
    case_revision_mismatch.results[0].case_identity_sha256 = _sha(
        "case:case-a:revision-2:QUERY"
    )

    assert compare_reports(baseline, revision_mismatch).reason == (
        "shared_selection_provenance_mismatch"
    )
    assert compare_reports(baseline, dataset_mismatch).reason == (
        "shared_selection_provenance_mismatch"
    )
    assert compare_reports(baseline, partial).reason == "partial_run"
    assert compare_reports(baseline, mode_mismatch).reason == "result_mode_not_enabled"
    assert compare_reports(baseline, case_revision_mismatch).reason == (
        "pair_set_mismatch_missing_1_extra_1"
    )
    assert compare_reports(baseline, repetition_mismatch).reason == (
        "incomplete_repetition_evidence"
    )


def test_keyed_comparison_refuses_legacy_or_incomplete_identity() -> None:
    baseline = _report("baseline", {("case-a", 0): True})
    legacy = copy.deepcopy(_report("legacy", {("case-a", 0): True}))
    legacy.results[0].case_identity_sha256 = ""
    incomplete = copy.deepcopy(_report("incomplete", {("case-a", 0): True}))
    incomplete.execution_evidence["executed_occurrences"] = 0

    assert compare_reports(baseline, legacy).reason == "result_missing_pair_identity"
    assert compare_reports(baseline, incomplete).reason == "incomplete_execution_coverage"


def test_keyed_comparison_reconciles_each_repetition_with_result_counts() -> None:
    baseline = _report(
        "baseline",
        {("case-a", 0): True, ("case-a", 1): False},
    )
    malformed = copy.deepcopy(
        _report(
            "malformed",
            {("case-a", 0): True, ("case-a", 1): False},
        )
    )
    malformed.execution_evidence["runs"][1]["executed_occurrences"] = 0

    comparison = compare_reports(baseline, malformed)

    assert comparison.status == "REFUSED"
    assert comparison.reason == "invalid_repetition_evidence"


def test_comparison_report_emits_deterministic_json_manifest_and_no_p_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import benchmarks.runner as runner

    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    baseline = _report("baseline", {("case-a", 0): False})
    candidate = _report("candidate", {("case-a", 0): True})

    _generate_comparison_report("synthetic", {"baseline": baseline, "candidate": candidate})
    first = (tmp_path / "comparison-synthetic.json").read_text(encoding="utf-8")
    markdown = (tmp_path / "comparison-synthetic.md").read_text(encoding="utf-8")
    _generate_comparison_report("synthetic", {"baseline": baseline, "candidate": candidate})
    second = (tmp_path / "comparison-synthetic.json").read_text(encoding="utf-8")

    assert first == second
    assert json.loads(first)["comparisons"][0]["status"] == "PAIRED_DESCRIPTIVE"
    assert "Statistical Significance" not in markdown
    assert "McNemar p" not in markdown
    assert "bootstrap" not in markdown


@pytest.mark.asyncio
async def test_run_experiment_emits_typed_paired_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import benchmarks.runner as runner

    cases = [
        BenchmarkCase(
            case_id="case-a",
            source="synthetic",
            layer="L1",
            domain="orders",
            question="case a",
            gold_sql="SELECT 1",
            gold_value=1,
        ),
        BenchmarkCase(
            case_id="case-b",
            source="synthetic",
            layer="L1",
            domain="orders",
            question="case b",
            gold_sql="SELECT 2",
            gold_value=2,
        ),
    ]
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "load_cases", lambda *_args, **_kwargs: list(cases))
    monkeypatch.setattr(
        runner,
        "EXPERIMENT_CONFIGS",
        {
            "synthetic-paired": [
                {
                    "name": "baseline",
                    "description": "baseline",
                    "overrides": {"model": "baseline-model"},
                },
                {
                    "name": "candidate",
                    "description": "candidate",
                    "overrides": {"model": "candidate-model"},
                },
            ]
        },
    )
    resolved_config = {"model": "unused-model"}

    @contextlib.contextmanager
    def override_config(overrides: dict[str, Any]):
        previous = resolved_config["model"]
        resolved_config["model"] = overrides["model"]
        try:
            yield
        finally:
            resolved_config["model"] = previous

    monkeypatch.setattr(runner, "override_agent_config", override_config)
    monkeypatch.setattr(
        runner,
        "_effective_runtime_config_sha256",
        lambda: _sha(resolved_config["model"]),
    )

    def fake_build_typed_executor(typed_runtime: str) -> _TypedHarnessExecutor:
        assert typed_runtime == "demo"
        return _TypedHarnessExecutor(resolved_config["model"])

    monkeypatch.setattr(runner, "build_typed_executor", fake_build_typed_executor)
    reports = await runner.run_experiment(
        "synthetic-paired", dataset="synthetic", typed_runtime="demo"
    )

    assert set(reports) == {"baseline", "candidate"}
    baseline, candidate = reports["baseline"], reports["candidate"]
    assert baseline.evidence_kind == candidate.evidence_kind == "harness"
    assert baseline.accuracy_established is True
    assert candidate.accuracy_established is True
    assert baseline.execution_evidence["pair_identity_state"] == "VALID"
    assert candidate.execution_evidence["pair_identity_state"] == "VALID"
    assert baseline.manifest["case_selection_checksum"] == candidate.manifest[
        "case_selection_checksum"
    ]
    payload = json.loads(
        (tmp_path / "comparison-synthetic-paired.json").read_text(encoding="utf-8")
    )
    markdown = (tmp_path / "comparison-synthetic-paired.md").read_text(encoding="utf-8")
    comparison = payload["comparisons"][0]
    assert comparison["status"] == "PAIRED_DESCRIPTIVE"
    assert comparison["pair_count"] == 2
    assert comparison["adjudicated_pair_count"] == 2
    assert comparison["unknown_pair_count"] == 0
    assert comparison["mean_paired_pass_delta"] == -0.5
    assert comparison["evidence_kind"] == "harness"
    assert {item["evidence_kind"] for item in payload["variants"]} == {"harness"}
    assert "case-a" not in json.dumps(payload)
    assert "candidate-model" not in json.dumps(payload)
    assert comparison["manifest"]["baseline"]["runtime_config_sha256"] != (
        comparison["manifest"]["candidate"]["runtime_config_sha256"]
    )
    assert all(result.case_identity_sha256 == "" for report in reports.values() for result in report.results)
    for report in reports.values():
        persisted = json.loads(
            (tmp_path / f"{report.run_id}.json").read_text(encoding="utf-8")
        )
        serialized_run = json.dumps(persisted)
        markdown_run = (tmp_path / f"{report.run_id}.md").read_text(encoding="utf-8")
        assert "case_identity_sha256" not in serialized_run
        assert '"sample_id"' not in serialized_run
        assert "Sample ID" not in markdown_run
        for case_id in ("case-a", "case-b"):
            per_case_hash = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
            assert per_case_hash not in serialized_run
            assert per_case_hash not in markdown_run
    assert "COMPLETE" in markdown
    assert "harness" in markdown
    assert "MEASURED" in markdown


def test_experiment_cli_defaults_to_fail_closed_and_hides_unestablished_accuracy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    import benchmarks.runner as runner

    cases = [
        BenchmarkCase(
            case_id="case-a",
            source="synthetic",
            layer="L1",
            domain="orders",
            question="case a",
            gold_sql="SELECT 1",
            gold_value=1,
        ),
        BenchmarkCase(
            case_id="case-b",
            source="synthetic",
            layer="L1",
            domain="orders",
            question="case b",
            gold_sql="SELECT 2",
            gold_value=2,
        ),
    ]
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "load_cases", lambda *_args, **_kwargs: list(cases))
    monkeypatch.setattr(
        runner,
        "EXPERIMENT_CONFIGS",
        {
            "synthetic-paired": [
                {"name": "baseline", "description": "baseline", "overrides": {}},
                {"name": "candidate", "description": "candidate", "overrides": {}},
            ]
        },
    )
    monkeypatch.setattr(
        runner, "_effective_runtime_config_sha256", lambda: _sha("production")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark-runner",
            "--experiment",
            "synthetic-paired",
            "--dataset",
            "enterprise",
        ],
    )

    runner.main()

    output = capsys.readouterr().out
    assert "accuracy=not established" in output
    assert "INCOMPLETE_RECEIPT_COVERAGE" in output
    assert "typed_runtime_unavailable" in output
    assert "EX=0.00%" not in output
    payload = json.loads(
        (tmp_path / "comparison-synthetic-paired.json").read_text(encoding="utf-8")
    )
    assert all(item["accuracy_established"] is False for item in payload["variants"])
    assert all(
        item["evidence_kind"] == "typed_runtime_unavailable"
        for item in payload["variants"]
    )
    assert payload["comparisons"][0]["unknown_pair_count"] == 2


@pytest.mark.asyncio
async def test_partial_experiment_does_not_persist_unexecuted_case_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import benchmarks.runner as runner

    cases = [
        BenchmarkCase(
            case_id="case-a",
            source="synthetic",
            layer="L1",
            domain="orders",
            question="case a",
            gold_sql="SELECT 1",
            gold_value=1,
        ),
        BenchmarkCase(
            case_id="case-b",
            source="synthetic",
            layer="L1",
            domain="orders",
            question="case b",
            gold_sql="SELECT 2",
            gold_value=2,
        ),
    ]
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(runner, "load_cases", lambda *_args, **_kwargs: list(cases))
    monkeypatch.setattr(
        runner,
        "EXPERIMENT_CONFIGS",
        {
            "synthetic-partial": [
                {"name": "baseline", "description": "baseline", "overrides": {}},
                {"name": "candidate", "description": "candidate", "overrides": {}},
            ]
        },
    )
    monkeypatch.setattr(
        runner, "build_typed_executor", lambda _runtime: _OverBudgetTypedExecutor()
    )
    monkeypatch.setattr(
        runner, "_effective_runtime_config_sha256", lambda: _sha("harness")
    )
    case_hashes = {
        runner._case_identity_sha256(case) for case in runner.to_eval_cases(cases)
    }

    reports = await runner.run_experiment(
        "synthetic-partial", dataset="synthetic", typed_runtime="demo"
    )

    assert set(reports) == {"baseline", "candidate"}
    for report in reports.values():
        assert report.execution_status == "PARTIAL_BUDGET_EXHAUSTED"
        assert all(
            "unexecuted_occurrence_sha256" not in run
            for run in report.execution_evidence["runs"]
        )
        persisted = json.dumps(
            json.loads(
                (tmp_path / f"{report.run_id}.json").read_text(encoding="utf-8")
            )
        )
        assert "unexecuted_occurrence_sha256" not in persisted
        assert all(case_hash not in persisted for case_hash in case_hashes)


def test_legacy_positional_helpers_refuse_mismatched_array_lengths() -> None:
    with pytest.raises(ValueError, match="equal lengths"):
        statistical_significance([1.0, 0.0], [1.0])
    with pytest.raises(ValueError, match="equal lengths"):
        paired_bootstrap_interval([1.0, 0.0], [1.0])
    with pytest.raises(ValueError, match="equal lengths"):
        mcnemar_exact([True, False], [True])


def test_effective_config_fingerprint_redacts_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.settings as core_settings
    import src.nl2sql.config.settings as agent_settings
    from benchmarks.runner import _effective_runtime_config_sha256

    first = agent_settings.AgentConfig(
        embedding_api_key=SecretStr("secret-value-one")
    )
    second = agent_settings.AgentConfig(
        embedding_api_key=SecretStr("secret-value-two")
    )
    absent = agent_settings.AgentConfig(
        embedding_api_key=SecretStr("not-needed")
    )
    monkeypatch.setattr(
        core_settings,
        "get_settings",
        lambda: SimpleNamespace(
            openai_api_key="",
            openai_base_url="https://legacy.example",
            model_name="legacy-model",
        ),
    )
    for environment_name in (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY_FILE",
        "NVIDIA_API_KEY",
        "NVIDIA_API_KEY_FILE",
    ):
        monkeypatch.delenv(environment_name, raising=False)

    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: first)
    first_fingerprint = _effective_runtime_config_sha256()
    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: second)
    second_fingerprint = _effective_runtime_config_sha256()
    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: absent)
    absent_fingerprint = _effective_runtime_config_sha256()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "runtime-provider-secret")
    configured_fingerprint = _effective_runtime_config_sha256()

    assert first_fingerprint == second_fingerprint
    assert first_fingerprint != absent_fingerprint
    assert "secret-value" not in first_fingerprint
    assert absent_fingerprint != configured_fingerprint
    assert "runtime-provider-secret" not in configured_fingerprint


def test_config_override_logs_never_include_secret_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import src.nl2sql.config.settings as agent_settings
    from benchmarks.agent_bridge import override_agent_config

    config = SimpleNamespace(api_key="original-secret")
    monkeypatch.setattr(agent_settings, "get_agent_config", lambda: config)
    caplog.set_level(logging.INFO)

    with override_agent_config({"api_key": "candidate-secret"}):
        assert config.api_key == "candidate-secret"

    assert config.api_key == "original-secret"
    assert "candidate-secret" not in caplog.text
    assert "original-secret" not in caplog.text
