from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks import runner
from benchmarks.adapters import BenchmarkCase
from benchmarks.registry import EvalCase, canonical_json, sha256_hex
from benchmarks.runner import run_typed_dataset
from benchmarks.selection import SelectionRun, verify_selection_checksum
from benchmarks.typed_receipts import ProviderCallReceipt, TypedAnswerReceipt


def _cases() -> list[BenchmarkCase]:
    return [
        BenchmarkCase(
            case_id="shared-case",
            source="unit",
            layer="L1",
            domain="typed",
            question="query example",
            gold_value=3,
        ),
        BenchmarkCase(
            case_id="shared-case",
            source="unit",
            layer="L2",
            domain="typed",
            question="analyze example",
            expected_mode="sql_plus_code",
            gold_value=5,
        ),
    ]


def _receipt(
    case: BenchmarkCase | EvalCase,
    *,
    with_model_call: bool = True,
    cost: float = 0.01,
    provider_calls: int = 1,
) -> TypedAnswerReceipt:
    if isinstance(case, EvalCase):
        value = case.oracle.expected_value if case.oracle is not None else None
    else:
        value = case.gold_value
    call_receipt = ProviderCallReceipt(
        alias="fast.default",
        stage="answer",
        resolved_model="test-model",
        input_tokens=2,
        output_tokens=1,
        estimated_cost=cost,
        latency_ms=2,
    )
    calls = tuple(call_receipt for _ in range(provider_calls)) if with_model_call else ()
    return TypedAnswerReceipt(
        trace_id="trace-repetition-test",
        answer_type="answer",
        answer_hash="a" * 64,
        rowset_sha256="b" * 64,
        candidate_score=1.0,
        policy_outcome="allow",
        execution_accepted=True,
        execution_row_count=1,
        result_value=value,
        model_calls=calls,
    )


def _install_dataset(
    monkeypatch: pytest.MonkeyPatch,
    cases: list[BenchmarkCase],
    executor: runner.TypedExecutor,
) -> None:
    monkeypatch.setattr(runner, "load_cases", lambda dataset, max_cases=None: cases)
    monkeypatch.setattr(runner, "build_typed_executor", lambda typed_runtime: executor)


@pytest.mark.parametrize("repetitions", [1, 3])
def test_run_typed_dataset_executes_each_selected_occurrence(
    monkeypatch: pytest.MonkeyPatch, repetitions: int
) -> None:
    cases = _cases()
    calls: list[tuple[str, str, str]] = []

    async def executor(item: runner.CaseInput) -> TypedAnswerReceipt:
        assert isinstance(item, EvalCase)
        calls.append((item.case_id, item.mode, item.revision))
        return _receipt(item)

    _install_dataset(monkeypatch, cases, executor)
    report, plan = run_typed_dataset(
        dataset="unit",
        changes=["not.a.known.change"],
        seed=17,
        repetitions=repetitions,
        max_cases=1,
        typed_runtime="demo",
        run_id=f"repetition-{repetitions}",
    )

    case_by_key = {runner.case_key(case): case for case in plan.cases}
    expected = [
        (case_by_key[key].case_id, case_by_key[key].mode, case_by_key[key].revision)
        for selection_run in plan.runs
        for key in selection_run.case_keys
    ]
    assert plan.conservative is True
    assert plan.sampling_skipped is True
    assert calls == expected
    assert len(calls) == len(plan.runs) * len(plan.cases)
    assert report.execution_status == "COMPLETE"
    assert report.total_cases == len(expected)
    assert report.execution_evidence["planned_occurrences"] == len(expected)
    assert report.execution_evidence["executed_occurrences"] == len(expected)
    assert report.execution_evidence["executor_invocations"] == len(expected)
    assert report.execution_evidence["provider_calls"] == len(expected)
    assert report.execution_evidence["provider_cost"] == pytest.approx(
        0.01 * len(expected)
    )
    assert report.accuracy_established is True

    # Same case ID under different revisions/modes remains two independent
    # selection identities; repetitions add a separate run identity.
    mode_identities = {
        (result.mode, result.case_identity_sha256) for result in report.results
    }
    assert len(mode_identities) == 2
    assert {result.mode for result in report.results} == {"QUERY", "ANALYZE"}
    assert len({result.execution_run_id for result in report.results}) == repetitions
    for result in report.results:
        assert result.selection_checksum == plan.checksum
        assert result.repetition_index is not None
        assert result.repetition_seed is not None
        assert len(result.repetition_checksum) == 64
        assert len(result.case_identity_sha256) == 64
    for run_evidence, selection_run in zip(
        report.execution_evidence["runs"], plan.runs, strict=True
    ):
        assert run_evidence["repetition_index"] == selection_run.index
        assert run_evidence["repetition_seed"] == selection_run.seed
        assert run_evidence["repetition_checksum"] == selection_run.checksum
        assert run_evidence["executor_invocations"] == len(selection_run.case_keys)
        assert run_evidence["provider_calls"] == len(selection_run.case_keys)
        assert run_evidence["provider_cost"] == pytest.approx(
            0.01 * len(selection_run.case_keys)
        )


def test_seed_and_run_id_reproduce_execution_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = _cases()

    async def executor(item: runner.CaseInput) -> TypedAnswerReceipt:
        assert isinstance(item, EvalCase)
        return _receipt(item)

    _install_dataset(monkeypatch, cases, executor)
    first, first_plan = run_typed_dataset(
        dataset="unit",
        changes=["unknown-change"],
        seed=23,
        repetitions=3,
        typed_runtime="demo",
        run_id="reproducible-run",
    )
    second, second_plan = run_typed_dataset(
        dataset="unit",
        changes=["unknown-change"],
        seed=23,
        repetitions=3,
        typed_runtime="demo",
        run_id="reproducible-run",
    )

    assert first_plan.checksum == second_plan.checksum
    assert first.execution_evidence == second.execution_evidence
    assert [result.execution_run_id for result in first.results] == [
        result.execution_run_id for result in second.results
    ]
    assert [result.case_identity_sha256 for result in first.results] == [
        result.case_identity_sha256 for result in second.results
    ]


def test_conflicting_revision_for_same_case_and_mode_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [
        _cases()[0],
        BenchmarkCase(
            case_id="shared-case",
            source="unit",
            layer="L1",
            domain="typed",
            question="different revision under the same mode",
            gold_value=99,
        ),
    ]

    async def executor(item: runner.CaseInput) -> TypedAnswerReceipt:
        assert isinstance(item, EvalCase)
        return _receipt(item)

    _install_dataset(monkeypatch, cases, executor)
    with pytest.raises(ValueError, match="conflicting revisions"):
        run_typed_dataset(
            dataset="unit",
            repetitions=2,
            typed_runtime="demo",
            run_id="conflicting-case-revision",
        )


@pytest.mark.parametrize("omission", ["mandatory", "selected"])
def test_recomputed_but_incomplete_run_plan_is_rejected_before_executor(
    monkeypatch: pytest.MonkeyPatch,
    omission: str,
) -> None:
    cases = [
        *_cases(),
        BenchmarkCase(
            case_id="mandatory-cross-cutting",
            source="unit",
            layer="L4",
            domain="safety",
            question="shared regression case",
            tags=["shared_regression"],
        ),
    ]
    calls: list[str] = []

    async def executor(item: runner.CaseInput) -> TypedAnswerReceipt:
        assert isinstance(item, EvalCase)
        calls.append(item.case_id)
        return _receipt(item)

    _install_dataset(monkeypatch, cases, executor)
    executor_builds: list[str] = []
    monkeypatch.setattr(
        runner,
        "build_typed_executor",
        lambda runtime: executor_builds.append(runtime) or executor,
    )
    original_select = runner.select_cases
    malformed_plans = []

    def select_malformed(candidates, **kwargs):  # type: ignore[no-untyped-def]
        plan = original_select(candidates, **kwargs)
        mandatory = set(plan.shared_regression_keys)
        missing = (
            next(iter(mandatory))
            if omission == "mandatory"
            else next(iter(set(plan.case_keys()) - mandatory))
        )
        changed_runs: list[SelectionRun] = []
        for run in plan.runs:
            case_keys = tuple(key for key in run.case_keys if key != missing)
            run_checksum = sha256_hex(
                canonical_json(
                    {
                        "selection_version": plan.selection_version,
                        "index": run.index,
                        "seed": run.seed,
                        "case_keys": list(case_keys),
                    }
                )
            )
            changed_runs.append(
                replace(run, case_keys=case_keys, checksum=run_checksum)
            )
        malformed = replace(plan, runs=tuple(changed_runs), checksum="")
        malformed = replace(malformed, checksum=malformed.recompute_checksum())
        assert verify_selection_checksum(malformed)
        malformed_plans.append(malformed)
        return malformed

    monkeypatch.setattr(runner, "select_cases", select_malformed)
    with pytest.raises(ValueError, match="selection run"):
        run_typed_dataset(
            dataset="unit",
            changes=["unknown-change"],
            seed=13,
            repetitions=2,
            typed_runtime="demo",
            run_id=f"malformed-{omission}",
        )

    assert calls == []
    assert executor_builds == []
    assert len(malformed_plans) == 1
    assert verify_selection_checksum(malformed_plans[0]) is True


def test_missing_receipt_stays_provenance_failure_in_repetition_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [_cases()[0]]
    calls = 0

    async def executor(item: runner.CaseInput) -> TypedAnswerReceipt | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        assert isinstance(item, EvalCase)
        return _receipt(item)

    _install_dataset(monkeypatch, cases, executor)
    report, _ = run_typed_dataset(
        dataset="unit",
        seed=9,
        repetitions=2,
        typed_runtime="demo",
        run_id="missing-receipt-repetition",
    )

    assert calls == 2
    assert report.total_cases == 2
    assert report.denominators["adjudicated_cases"] == 2
    assert report.outcome_counts["PROVENANCE_FAILURE"] == 1
    assert report.results[0].observed_outcome == "PROVENANCE_FAILURE"
    assert report.results[1].passed is True


def test_zero_call_receipts_cannot_bypass_executor_invocation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [_cases()[0]]
    calls: list[str] = []

    async def executor(item: runner.CaseInput) -> TypedAnswerReceipt:
        assert isinstance(item, EvalCase)
        calls.append(item.case_id)
        return _receipt(item, with_model_call=False)

    _install_dataset(monkeypatch, cases, executor)
    report, plan = run_typed_dataset(
        dataset="unit",
        seed=4,
        repetitions=4,
        typed_runtime="demo",
        run_id="invocation-budget",
        max_executor_invocations=2,
    )

    assert len(plan.runs) == 4
    assert calls == ["shared-case", "shared-case"]
    assert report.execution_status == "PARTIAL_BUDGET_EXHAUSTED"
    assert report.execution_evidence["executor_invocations"] == 2
    assert report.execution_evidence["planned_occurrences"] == 4
    assert report.execution_evidence["executed_occurrences"] == 2
    assert report.execution_evidence["unexecuted_occurrences"] == 2
    assert report.execution_evidence["provider_calls"] == 0
    assert report.execution_evidence["completed_repetitions"] == 2
    assert report.accuracy_established is False
    assert report.accuracy_evidence_state == "PARTIAL_EXECUTION"
    assert report.execution_evidence["runs"][-1]["status"] == (
        "NOT_STARTED_BUDGET_EXHAUSTED"
    )
    assert report.execution_evidence["runs"][-1]["unexecuted_occurrence_sha256"]
    assert "shared-case" not in json.dumps(report.execution_evidence)


def test_provider_budget_is_cumulative_and_stops_before_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [_cases()[0]]
    calls: list[str] = []

    async def executor(item: runner.CaseInput) -> TypedAnswerReceipt:
        assert isinstance(item, EvalCase)
        calls.append(item.case_id)
        return _receipt(item, cost=0.01)

    _install_dataset(monkeypatch, cases, executor)
    monkeypatch.setattr(runner, "DEFAULT_BUDGET_CALLS", 1)
    report, plan = run_typed_dataset(
        dataset="unit",
        seed=5,
        repetitions=3,
        typed_runtime="demo",
        run_id="provider-budget",
    )

    assert calls == ["shared-case"]
    assert report.execution_status == "PARTIAL_BUDGET_EXHAUSTED"
    assert report.execution_evidence["provider_calls"] == 1
    assert report.execution_evidence["provider_cost"] == 0.01
    assert report.execution_evidence["planned_occurrences"] == 3
    assert report.execution_evidence["executed_occurrences"] == 1
    assert report.execution_evidence["unexecuted_occurrences"] == 2
    assert report.execution_evidence["runs"][1]["budget_exhaustion_phase"] == (
        "before_executor_invocation"
    )
    assert report.manifest["case_selection_checksum"] == plan.checksum
    assert report.accuracy_established is False


def test_post_receipt_budget_overrun_is_recorded_and_stops_later_occurrences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = [_cases()[0]]
    calls: list[str] = []

    async def executor(item: runner.CaseInput) -> TypedAnswerReceipt:
        assert isinstance(item, EvalCase)
        calls.append(item.case_id)
        return _receipt(item, cost=0.01, provider_calls=2)

    _install_dataset(monkeypatch, cases, executor)
    monkeypatch.setattr(runner, "DEFAULT_BUDGET_CALLS", 1)
    monkeypatch.setattr(runner, "DEFAULT_BUDGET_COST", 0.015)
    report, _ = run_typed_dataset(
        dataset="unit",
        seed=6,
        repetitions=3,
        typed_runtime="demo",
        run_id="post-receipt-budget-overrun",
    )

    assert calls == ["shared-case"]
    assert report.execution_status == "PARTIAL_BUDGET_EXHAUSTED"
    assert report.execution_evidence["executor_invocations"] == 1
    assert report.execution_evidence["provider_calls"] == 2
    assert report.execution_evidence["provider_call_limit"] == 1
    assert report.execution_evidence["provider_call_overrun"] == 1
    assert report.execution_evidence["provider_cost"] == 0.02
    assert report.execution_evidence["provider_cost_limit"] == 0.015
    assert report.execution_evidence["provider_cost_overrun"] == 0.005
    assert report.total_provider_cost == 0.02
    assert report.execution_evidence["executed_occurrences"] == 1
    assert report.execution_evidence["unexecuted_occurrences"] == 2
    assert report.execution_evidence["runs"][0]["status"] == (
        "PARTIAL_BUDGET_EXHAUSTED"
    )
    assert report.execution_evidence["runs"][0]["budget_exhaustion_phase"] == (
        "after_executor_receipt"
    )
    assert report.execution_evidence["runs"][1]["status"] == (
        "NOT_STARTED_BUDGET_EXHAUSTED"
    )
    assert report.accuracy_established is False
    assert report.accuracy_evidence_state == "PARTIAL_EXECUTION"


def test_cli_executes_repetitions_and_persists_redacted_run_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases = _cases()
    calls: list[str] = []

    async def executor(item: runner.CaseInput) -> TypedAnswerReceipt:
        assert isinstance(item, EvalCase)
        calls.append(item.case_id)
        return _receipt(item)

    _install_dataset(monkeypatch, cases, executor)
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmarks.runner",
            "--dataset",
            "p9a_typed",
            "--typed-runtime",
            "demo",
            "--run-id",
            "cli-repetitions",
            "--change",
            "unknown-change",
            "--repetitions",
            "3",
            "--output-dir",
            str(tmp_path),
        ],
    )

    runner.main()
    output = capsys.readouterr().out
    payload_path = next(tmp_path.glob("*.json"))
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    markdown = payload_path.with_suffix(".md").read_text(encoding="utf-8")

    assert len(calls) == 6
    assert "repetitions=3" in output
    assert "execution_status: COMPLETE" in output
    assert payload["execution_status"] == "COMPLETE"
    assert payload["execution_evidence"]["requested_repetitions"] == 3
    assert payload["execution_evidence"]["planned_occurrences"] == 6
    assert payload["execution_evidence"]["executed_occurrences"] == 6
    assert len(payload["execution_evidence"]["runs"]) == 3
    assert len(payload["case_details"]) == 6
    assert all("case_identity_sha256" in item for item in payload["case_details"])
    assert "shared-case" not in payload_path.read_text(encoding="utf-8")
    assert "shared-case" not in markdown
    assert "## Repetition Execution" in markdown
