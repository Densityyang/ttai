"""P9A unified evaluation foundation tests.

These tests live under benchmarks/ because P9A is only allowed to change
benchmarks/ (plus the CI and pyright configuration).  They are the
execution-level evidence for the P9A must-test list in
MASTER_PR_PLAN_V4.md 8.17.

Nothing here claims a real accuracy number: fake providers and the stub only
prove the harness.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from benchmarks.adapters import (
    BenchmarkCase,
    benchmark_case_to_eval_case,
    load_bird_eval_cases,
    load_enterprise_cases,
    load_enterprise_eval_cases,
)
from benchmarks.assertions import (
    CaseObservation,
    assert_privacy_redaction,
    evaluate_case,
    values_match,
)
from benchmarks.metrics import (
    CaseResult,
    adjudicate_case_result,
    compute_denominators,
    compute_execution_accuracy,
    generate_report,
)
from benchmarks.registry import (
    OUTCOME_TAXONOMY,
    CaseOracle,
    EvalCase,
    build_registry,
    dedupe_cases,
)
from benchmarks.runner import load_cases, run_benchmark, run_typed_benchmark
from benchmarks.typed_receipts import (
    BenchmarkManifest,
    BudgetExceeded,
    BudgetGate,
    ProviderCallReceipt,
    TypedAnswerReceipt,
    upcast_legacy_manifest,
    upcast_manifest_file,
)

DATASETS = Path(__file__).resolve().parent.parent / "datasets"
ENTERPRISE = DATASETS / "enterprise"
BIRD = DATASETS / "bird"
_SHA = "a" * 64


def _manifest(**overrides: object) -> BenchmarkManifest:
    payload: dict[str, object] = {
        "run_id": "p9a-test-run",
        "dataset_checksum": _SHA,
        "prompt_version": "prompt-v1",
        "policy_version": "policy-v1",
        "semantic_version": "semantic-v1",
        "model_profile_version": "profile-v1",
        "git_revision": "abcdef0",
    }
    payload.update(overrides)
    return BenchmarkManifest.model_validate(payload)


def _receipt(**overrides: object) -> TypedAnswerReceipt:
    payload: dict[str, object] = {
        "trace_id": "trace-1",
        "answer_type": "answer",
        "answer_hash": "b" * 64,
        "rowset_sha256": "c" * 64,
        "candidate_score": 0.9,
        "policy_outcome": "allow",
        "execution_accepted": True,
        "execution_row_count": 1,
        "result_value": 42,
        "model_calls": (
            ProviderCallReceipt(
                alias="fast.default",
                stage="answer",
                resolved_model="fake-model",
                input_tokens=3,
                output_tokens=2,
                estimated_cost=0.01,
                latency_ms=10,
            ),
        ),
    }
    payload.update(overrides)
    return TypedAnswerReceipt.model_validate(payload)


def _answer_case(*, expected_value: object = 42, case_id: str = "case-1") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        revision="rev-1",
        source="unit",
        layer="L1",
        domain="test",
        question="how many?",
        expected_outcome="CORRECT_ANSWER",
        oracle=CaseOracle(
            oracle_kind="approved_fact",
            oracle_revision="oracle-r1",
            expected_value=expected_value,
        ),
    )


def _label_case(expected_outcome: str, *, case_id: str = "label-1") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        revision="rev-1",
        source="unit",
        layer="L4",
        domain="robustness",
        question="ambiguous request",
        expected_outcome=expected_outcome,  # type: ignore[arg-type]
        oracle=CaseOracle(oracle_kind="reviewed_label", oracle_revision="oracle-r1"),
    )


# ── S1: registry, revision/checksum, dedup, legacy consumers ────────────────


def test_registry_loads_enterprise_40_and_bird_1534() -> None:
    enterprise = load_enterprise_eval_cases(ENTERPRISE)
    bird = load_bird_eval_cases(BIRD)
    assert len(enterprise) == 40
    assert len(bird) == 1534
    registry = build_registry([*enterprise, *bird])
    assert len(registry.cases) == 1574
    assert registry.registry_revision == "p9a-registry-v1"


def test_registry_revision_and_checksum_are_stable_for_identical_input() -> None:
    first = build_registry(load_enterprise_eval_cases(ENTERPRISE))
    second = build_registry(load_enterprise_eval_cases(ENTERPRISE))
    assert first.checksum == second.checksum
    revisions_a = [case.revision for case in first.cases]
    revisions_b = [case.revision for case in second.cases]
    assert revisions_a == revisions_b


def test_legacy_benchmark_case_consumers_are_not_broken() -> None:
    cases = load_enterprise_cases(ENTERPRISE)
    assert len(cases) == 40
    assert all(isinstance(case, BenchmarkCase) for case in cases)
    case_ids = {case.case_id for case in cases}
    assert "ent-L1-001" in case_ids
    assert "hitl-001" in case_ids
    first = cases[0]
    assert first.layer in {"L1", "L2", "L3", "L4"}
    assert first.question
    # The new field is additive and defaults to empty.
    assert isinstance(first.expected_outcome, str)


def test_shared_case_dedup_keeps_mode_variants() -> None:
    query_case = benchmark_case_to_eval_case(
        BenchmarkCase(case_id="shared", source="unit", layer="L1", domain="d", question="q")
    )
    build_case = query_case.model_copy(update={"mode": "BUILD"})
    deduped = dedupe_cases([query_case, build_case, query_case])
    assert len(deduped) == 2
    assert {case.mode for case in deduped} == {"QUERY", "BUILD"}


# ── S2: oracle gating, value correctness, receipts, plan, terminals ─────────


def test_missing_oracle_is_unknown_and_never_pass() -> None:
    case = EvalCase(
        case_id="no-oracle",
        revision="rev-1",
        source="unit",
        layer="L1",
        domain="test",
        question="q",
        expected_outcome="CORRECT_ANSWER",
        oracle=None,
    )
    verdict = evaluate_case(case, CaseObservation(case_id="no-oracle", execution_success=True))
    assert verdict.adjudicated is False
    assert verdict.passed is None
    assert verdict.observed_outcome == "UNKNOWN"
    assert verdict.failures == ()


def test_execution_success_but_wrong_value_is_fail() -> None:
    case = _answer_case(expected_value=100)
    verdict = evaluate_case(
        case,
        CaseObservation(case_id="case-1", execution_success=True, output_value=42),
    )
    assert verdict.adjudicated is True
    assert verdict.passed is False
    assert verdict.observed_outcome == "INCORRECT_ANSWER"
    assert "value_mismatch" in verdict.failures


def test_none_none_can_never_stand_in_for_a_correct_value() -> None:
    # The historical helper still says two absent values are equal; the
    # evaluator must never reach it when the oracle is absent.
    assert values_match(None, None) is True
    case = _answer_case(expected_value=0)
    verdict = evaluate_case(
        case,
        CaseObservation(case_id="case-1", execution_success=True, output_value=None),
    )
    assert verdict.passed is False
    assert "value_mismatch" in verdict.failures


def test_missing_receipt_is_provenance_failure() -> None:
    case = _answer_case()
    verdict = evaluate_case(
        case,
        CaseObservation(
            case_id="case-1",
            execution_success=True,
            output_value=42,
            receipt_required=True,
            receipt_present=False,
        ),
    )
    assert verdict.passed is False
    assert verdict.observed_outcome == "PROVENANCE_FAILURE"
    assert "receipt_missing" in verdict.failures


def test_confirmed_plan_hash_same_but_denominator_changed_is_deviation() -> None:
    case = _answer_case()
    changed = evaluate_case(
        case,
        CaseObservation(
            case_id="case-1",
            execution_success=True,
            output_value=42,
            confirmed_plan_checksum="plan-hash",
            observed_plan_checksum="plan-hash",
            expected_row_count=10,
            execution_row_count=7,
        ),
    )
    assert changed.passed is False
    assert changed.observed_outcome == "CONFIRMED_PLAN_DEVIATION"
    assert "confirmed_plan_denominator_changed" in changed.failures

    unchanged = evaluate_case(
        case,
        CaseObservation(
            case_id="case-1",
            execution_success=True,
            output_value=42,
            confirmed_plan_checksum="plan-hash",
            observed_plan_checksum="plan-hash",
            expected_row_count=10,
            execution_row_count=10,
        ),
    )
    assert unchanged.observed_outcome == "CORRECT_ANSWER"


def test_should_clarify_but_answered_is_incorrect() -> None:
    case = _label_case("CORRECT_CLARIFICATION")
    verdict = evaluate_case(
        case,
        CaseObservation(
            case_id="label-1",
            receipt_required=True,
            receipt_present=True,
            answer_type="answer",
            policy_outcome="allow",
            execution_accepted=True,
            execution_success=True,
        ),
    )
    assert verdict.passed is False
    assert verdict.observed_outcome == "INCORRECT_ANSWER"


def test_should_answer_but_refused_is_false_rejection() -> None:
    case = _answer_case()
    verdict = evaluate_case(
        case,
        CaseObservation(
            case_id="case-1",
            receipt_required=True,
            receipt_present=True,
            answer_type="rejected",
            policy_outcome="deny",
        ),
    )
    assert verdict.passed is False
    assert verdict.observed_outcome == "FALSE_REJECTION"


def test_one_case_produces_multiple_assertions() -> None:
    case = _answer_case()
    verdict = evaluate_case(
        case,
        CaseObservation(
            case_id="case-1",
            execution_success=True,
            output_value=42,
            receipt_required=True,
            receipt_present=True,
            answer_type="answer",
            policy_outcome="allow",
            execution_accepted=True,
        ),
    )
    names = {item.name for item in verdict.assertions}
    assert {
        "oracle_available",
        "mode_unchanged",
        "receipt_present",
        "confirmed_plan_unchanged",
        "expected_terminal",
        "oracle_value_match",
    } <= names
    assert verdict.passed is True


def test_outcome_taxonomy_matches_the_plan() -> None:
    assert len(OUTCOME_TAXONOMY) == 16  # 15 outcomes from 6.6.1 plus UNKNOWN
    for name in (
        "CORRECT_ANSWER",
        "CORRECT_CLARIFICATION",
        "CORRECT_HITL",
        "CORRECT_RESULT_UNAVAILABLE",
        "CORRECT_REJECTION",
        "INCORRECT_ANSWER",
        "INCORRECT_CONFIDENT_ANSWER",
        "FALSE_REJECTION",
        "UNNECESSARY_CLARIFICATION",
        "AUTHORIZATION_FAILURE",
        "SOURCE_ROUTING_FAILURE",
        "PROVENANCE_FAILURE",
        "DATA_QUALITY_FAILURE",
        "CONFIRMED_PLAN_DEVIATION",
        "SANDBOX_OR_MODEL_INPUT_FAILURE",
    ):
        assert name in OUTCOME_TAXONOMY
    assert OUTCOME_TAXONOMY[-1] == "UNKNOWN"


def test_statistics_denominator_excludes_unknown_cases() -> None:
    correct = CaseResult(
        case_id="ok",
        layer="L1",
        domain="d",
        expected_mode="sql_only",
        execution_success=True,
        output_value=10,
        gold_value=10,
    )
    wrong = CaseResult(
        case_id="wrong",
        layer="L1",
        domain="d",
        expected_mode="sql_only",
        execution_success=True,
        output_value=99,
        gold_value=10,
    )
    unknown = CaseResult(
        case_id="unknown",
        layer="L1",
        domain="d",
        expected_mode="sql_only",
        execution_success=True,
        output_value=None,
        gold_value=None,
    )
    results = [correct, wrong, unknown]
    assert compute_execution_accuracy(results) == 0.5
    denominators = compute_denominators(results)
    assert denominators["total_cases"] == 3
    assert denominators["adjudicated_cases"] == 2
    assert denominators["unknown_cases"] == 1
    assert denominators["unknown_missing_oracle"] == 1


def test_enterprise_stub_ex_is_no_longer_inflated() -> None:
    cases = load_cases("enterprise")
    assert len(cases) == 40
    report = asyncio.run(run_benchmark(cases, run_id="p9a-before-after", use_stub=True))

    legacy_correct = sum(
        1 for result in report.results if values_match(result.output_value, result.gold_value, result.tolerance)
    )
    legacy_ex = legacy_correct / len(report.results)
    assert round(legacy_ex, 4) == 0.975  # the pre-P9A inflated number
    assert report.execution_accuracy == 0.0  # adjudicated denominator, no fake PASS
    assert report.denominators["adjudicated_cases"] == 14
    assert report.denominators["unknown_cases"] == 26
    assert report.denominators["unknown_missing_oracle"] == 15
    assert report.denominators["unknown_reference_only"] == 11
    assert report.evidence_kind == "harness"


# ── S5: manifest/report contract and read-only migration ────────────────────


def test_manifest_no_longer_requires_three_providers() -> None:
    manifest = _manifest()
    assert manifest.matrix == ()
    assert manifest.schema_version == "1.0"
    # An explicit floor is still enforceable when a caller wants one.
    with pytest.raises(ValueError):
        _manifest(required_provider_count=1)


@pytest.mark.asyncio
async def test_report_is_produced_without_three_providers() -> None:
    cases = [
        BenchmarkCase(case_id="c1", source="unit", layer="L1", domain="d", question="q"),
    ]

    async def executor(case: object) -> TypedAnswerReceipt:
        return _receipt()

    report = await run_typed_benchmark(
        cases,
        manifest=_manifest(),
        executor=executor,
        budget=BudgetGate(max_total_cost=1.0, max_calls=5),
    )
    assert report.manifest["run_id"] == "p9a-test-run"
    assert report.manifest["schema_version"] == "1.0"


def test_legacy_manifest_upcast_is_read_only(tmp_path: Path) -> None:
    legacy = {
        "run_id": "legacy-run",
        "dataset_checksum": _SHA,
        "prompt_version": "prompt-v1",
        "policy_version": "policy-v1",
        "semantic_version": "semantic-v1",
        "model_profile_version": "profile-v1",
        "git_revision": "abcdef0",
        "matrix": ["deepseek.flash"],
        "an_unknown_legacy_field": {"keep": "me"},
    }
    path = tmp_path / "legacy-manifest.json"
    original = json.dumps(legacy, indent=2).encode("utf-8")
    path.write_bytes(original)

    manifest, digest = upcast_manifest_file(path)
    assert manifest.schema_version == "1.0"
    assert manifest.matrix == ("deepseek.flash",)
    assert manifest.legacy_extras["an_unknown_legacy_field"] == {"keep": "me"}
    assert digest
    assert path.read_bytes() == original  # history is byte-identical


def test_legacy_manifest_upcast_from_bytes_preserves_unknown_keys() -> None:
    raw = json.dumps(
        {
            "run_id": "legacy-run",
            "dataset_checksum": _SHA,
            "prompt_version": "prompt-v1",
            "policy_version": "policy-v1",
            "semantic_version": "semantic-v1",
            "model_profile_version": "profile-v1",
            "git_revision": "abcdef0",
            "matrix": ["a", "b"],
            "extra": 1,
        }
    )
    manifest = upcast_legacy_manifest(raw)
    assert manifest.legacy_extras == {"extra": 1}


def test_report_slices_three_modes() -> None:
    results = [
        CaseResult(
            case_id=f"case-{mode}",
            layer="L1",
            domain="d",
            expected_mode="sql_only",
            mode=mode,
            execution_success=True,
            output_value=1,
            gold_value=1,
        )
        for mode in ("QUERY", "ANALYZE", "BUILD")
    ]
    report = generate_report("slice-run", results)
    assert set(report.mode_slices) == {"QUERY", "ANALYZE", "BUILD"}
    assert report.for_mode("BUILD")["execution_accuracy"] == 1.0
    assert report.mode_accuracy["QUERY"] == 1.0
    assert report.to_dict()["mode_slices"]["ANALYZE"]["adjudicated_cases"] == 1


def test_report_slices_capability_and_risk() -> None:
    results = [
        CaseResult(
            case_id="fetch",
            layer="L1",
            domain="d",
            expected_mode="sql_only",
            capability="fetch",
            risk="low",
            execution_success=True,
            output_value=1,
            gold_value=1,
        ),
        CaseResult(
            case_id="safety",
            layer="L4",
            domain="security",
            expected_mode="reject",
            capability="safety",
            risk="high",
            is_adversarial=True,
            should_reject=True,
            was_intercepted=True,
        ),
    ]
    report = generate_report("cap-risk-run", results)
    assert report.capability_slices["fetch"]["execution_accuracy"] == 1.0
    assert report.risk_slices["high"]["adjudicated_cases"] == 1
    assert report.risk_slices["high"]["execution_accuracy"] == 1.0


# ── S6: budget precheck happens before the executor ─────────────────────────


@pytest.mark.asyncio
async def test_over_budget_case_never_calls_the_executor() -> None:
    calls = {"count": 0}

    async def executor(case: object) -> TypedAnswerReceipt:
        calls["count"] += 1
        return _receipt()

    cases = [
        BenchmarkCase(case_id=f"c{i}", source="unit", layer="L1", domain="d", question="q")
        for i in range(3)
    ]
    gate = BudgetGate(max_total_cost=0.0, max_calls=0)
    with pytest.raises(BudgetExceeded):
        await run_typed_benchmark(cases, manifest=_manifest(), executor=executor, budget=gate)
    assert calls["count"] == 0


def test_budget_precheck_and_reserve() -> None:
    gate = BudgetGate(max_total_cost=0.05, max_calls=2)
    gate.precheck(estimated_cost=0.01, estimated_calls=1)
    gate.reserve(estimated_cost=0.01, estimated_calls=1)
    with pytest.raises(BudgetExceeded, match="budget"):
        gate.precheck(estimated_calls=2)
    gate.release(estimated_cost=0.01, estimated_calls=1)
    gate.precheck(estimated_calls=2)


# ── privacy redaction ───────────────────────────────────────────────────────


def test_privacy_redaction_assertion() -> None:
    assert assert_privacy_redaction("plain report payload").passed is True
    secret = assert_privacy_redaction("provider_error: password=hunter2")
    assert secret.passed is False
    assert secret.failure_code == "sensitive_marker"
    raw_identity = assert_privacy_redaction("row for customer-secret-case", forbidden=["customer-secret-case"])
    assert raw_identity.passed is False
    assert raw_identity.failure_code == "raw_case_identity"


def test_adjudicated_result_carries_the_verdict() -> None:
    result = adjudicate_case_result(
        CaseResult(
            case_id="c",
            layer="L1",
            domain="d",
            expected_mode="sql_only",
            execution_success=True,
            output_value=5,
            gold_value=5,
        )
    )
    assert result.adjudicated is True
    assert result.passed is True
    assert result.observed_outcome == "CORRECT_ANSWER"
