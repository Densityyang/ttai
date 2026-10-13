from __future__ import annotations

import contextlib
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from benchmarks.adapters import BenchmarkCase
from benchmarks.comparison import compare_reports
from benchmarks.metrics import (
    BenchmarkReport,
    CaseResult,
    statistical_significance,
)
from benchmarks.runner import _generate_comparison_report
from benchmarks.typed_receipts import mcnemar_exact, paired_bootstrap_interval

_DATASET = "a" * 64
_SELECTION = "b" * 64


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
async def test_run_experiment_emits_reachable_keyed_comparison_for_reordered_results(
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
                {"name": "baseline", "description": "baseline", "overrides": {}},
                {
                    "name": "candidate",
                    "description": "candidate",
                    "overrides": {"model": "candidate-model"},
                },
            ]
        },
    )
    monkeypatch.setattr(runner, "override_agent_config", lambda _overrides: contextlib.nullcontext())
    monkeypatch.setattr(runner, "save_report", lambda _report: None)

    async def fake_run_benchmark(
        selected: list[BenchmarkCase], *, run_id: str, use_stub: bool
    ) -> BenchmarkReport:
        del use_stub
        selected_cases = list(selected)
        if "candidate" in run_id:
            selected_cases.reverse()
        results = [
            CaseResult(
                case_id=case.case_id,
                layer=case.layer,
                domain=case.domain,
                expected_mode=case.expected_mode,
                mode="QUERY",
                adjudicated=True,
                passed=True,
            )
            for case in selected_cases
        ]
        return BenchmarkReport(
            run_id=run_id,
            total_cases=len(results),
            results=results,
            accuracy_established=False,
            accuracy_evidence_state="NO_TYPED_RECEIPTS",
        )

    monkeypatch.setattr(runner, "run_benchmark", fake_run_benchmark)
    reports = await runner.run_experiment("synthetic-paired", dataset="synthetic")

    assert set(reports) == {"baseline", "candidate"}
    baseline, candidate = reports["baseline"], reports["candidate"]
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
    assert comparison["unknown_pair_count"] == 2
    assert comparison["mean_paired_pass_delta"] is None
    assert "case-a" not in json.dumps(payload)
    assert "COMPLETE" in markdown
    assert "NO_TYPED_RECEIPTS" in markdown
    assert "unavailable" in markdown


def test_legacy_positional_helpers_refuse_mismatched_array_lengths() -> None:
    with pytest.raises(ValueError, match="equal lengths"):
        statistical_significance([1.0, 0.0], [1.0])
    with pytest.raises(ValueError, match="equal lengths"):
        paired_bootstrap_interval([1.0, 0.0], [1.0])
    with pytest.raises(ValueError, match="equal lengths"):
        mcnemar_exact([True, False], [True])
