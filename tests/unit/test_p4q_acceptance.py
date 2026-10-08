"""P4-Q acceptance harness contract tests (fixture/contract-level only)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.nl2sql.contracts import (
    AnswerArtifact,
    AnswerFact,
    ExecutionPlan,
    ExecutionReceipt,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStepReceipt,
    TrustedCalculationStep,
    VerifyStep,
)
from src.nl2sql.orchestration.p4q_acceptance import (
    P4QCaseEvidence,
    P4QCaseRegistry,
    P4QCaseResult,
    P4QCaseSpec,
    P4QManifest,
    P4QOracleEvidence,
    P4QRealnessWitness,
    evaluate_p4q,
)
from src.nl2sql.semantic.published_reader import (
    PublishedMetricReadBinding,
    PublishedMetricReadEvidence,
    PublishedMetricReadOutcome,
    PublishedMetricReadReceipt,
    PublishedMetricReadRequest,
)
from src.nl2sql.semantic.published_result import (
    CURRENT_STATE,
    EffectivePublishedMetricResult,
    PublishedMetricKey,
)

AWARE = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
HEX = "a" * 64
OTHER = "b" * 64
FETCH_DIGEST = "c" * 64
CALC_DIGEST = "d" * 64
VERIFY_DIGEST = "e" * 64
REPO_ROOT = Path(__file__).resolve().parents[2]
REL_ID = UUID("22222222-2222-2222-2222-222222222222")
SNAP_ID = UUID("33333333-3333-3333-3333-333333333333")

KIND_TERMINAL = {
    "published_gold": "answered",
    "approved_compute": "answered",
    "clarification_resume": "answered",
    "authorization_denied": "denied",
    "missing": "missing",
    "unavailable": "unavailable",
}
KINDS = tuple(KIND_TERMINAL)


def _key(metric_code: str = "metric.a") -> PublishedMetricKey:
    return PublishedMetricKey(
        metric_code=metric_code,
        time_grain="month",
        time_value=CURRENT_STATE,
        dimension_type="all",
        area_id=None,
        team_id=None,
        employee_id=None,
        category_code="all",
    )


def _binding() -> PublishedMetricReadBinding:
    return PublishedMetricReadBinding(
        semantic_release_id="release-1",
        semantic_release_checksum=HEX,
        published_source_id="source-1",
        binding_revision="rev-1",
    )


def _result(
    key: PublishedMetricKey,
    status: str = "success",
    value: Decimal | None = Decimal("1"),
    **o: Any,
) -> EffectivePublishedMetricResult:
    base: dict[str, Any] = {
        "key": key,
        "status": status,
        "value": value,
        "value_type": "decimal",
        "unit": "count",
        "computed_at": AWARE,
        "effective_origin": "automated",
        "effective_revision": "rev-1",
    }
    base.update(o)
    return EffectivePublishedMetricResult(**base)


def _read_outcome(
    status: str = "success", value: Decimal | None = Decimal("1")
) -> PublishedMetricReadOutcome:
    key = _key()
    overrides: dict[str, Any] = {}
    if status == "missing":
        overrides = {
            "effective_origin": None,
            "effective_revision": None,
            "computed_at": None,
        }
    result = _result(key, status, value, **overrides)
    evidence = PublishedMetricReadEvidence(
        key=key,
        key_checksum=key.checksum,
        result_checksum=result.checksum,
        semantic_time=key.time_value,
        effective_origin=result.effective_origin,
        effective_revision=result.effective_revision,
        data_as_of=result.data_as_of,
        freshness_status=result.freshness_status,
        data_quality_score=result.data_quality_score,
    )
    request = PublishedMetricReadRequest(binding=_binding(), keys=(key,))
    receipt = PublishedMetricReadReceipt(
        binding=_binding(),
        started_at=AWARE,
        completed_at=AWARE,
        status="succeeded",
        evidence=(evidence,),
    )
    return PublishedMetricReadOutcome(request=request, results=(result,), receipt=receipt)


def _failed_read_outcome(failure_code: str = "source_unavailable") -> PublishedMetricReadOutcome:
    request = PublishedMetricReadRequest(binding=_binding(), keys=(_key(),))
    receipt = PublishedMetricReadReceipt(
        binding=_binding(),
        started_at=AWARE,
        completed_at=AWARE,
        status="failed",
        failure_code=failure_code,
    )
    return PublishedMetricReadOutcome(request=request, receipt=receipt)


def _plan(trusted: bool = False) -> ExecutionPlan:
    steps: list[Any] = [FetchMetricStep(step_id="s1", metric_keys=("metric.a",))]
    if trusted:
        steps.append(
            TrustedCalculationStep(
                step_id="s2",
                template_id="calc.ratio",
                input_refs={"value": "s1.value"},
                depends_on=("s1",),
            )
        )
    return ExecutionPlan(
        query_plan_sha256=HEX,
        semantic_release_id=REL_ID,
        schema_snapshot_id=SNAP_ID,
        policy_version="pol-1",
        steps=tuple(steps),
    )


def _step_receipt(
    step_id: str,
    kind: str,
    output_digest: str,
    rowset_sha256: str | None = None,
) -> PlanStepReceipt:
    return PlanStepReceipt(
        step_id=step_id,
        kind=kind,
        status="succeeded",
        elapsed_ms=5,
        output_digest=output_digest,
        rowset_sha256=rowset_sha256,
    )


def _record(
    plan: ExecutionPlan, *, trusted: bool = False, include_fetch: bool = True
) -> PlanExecutionRecord:
    receipts: list[PlanStepReceipt] = []
    ids: list[str] = []
    if include_fetch:
        receipts.append(_step_receipt("s1", "fetch_metric", FETCH_DIGEST, HEX))
        ids.append("s1")
    if trusted:
        receipts.append(_step_receipt("s2", "trusted_calculation", CALC_DIGEST))
        ids.append("s2")
    return PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=tuple(receipts),
        output_step_ids=tuple(ids),
    )


def _failed_record_with_succeeded_step(plan: ExecutionPlan) -> PlanExecutionRecord:
    return PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="failed",
        step_receipts=(_step_receipt("s1", "fetch_metric", FETCH_DIGEST, HEX),),
        output_step_ids=("s1",),
        stop_reason="boom",
    )


def _verify_only_record(plan: ExecutionPlan) -> PlanExecutionRecord:
    return PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(_step_receipt("s1", "verify", FETCH_DIGEST, HEX),),
        output_step_ids=("s1",),
    )


def _verify_plan() -> ExecutionPlan:
    return ExecutionPlan(
        query_plan_sha256=HEX,
        semantic_release_id=REL_ID,
        schema_snapshot_id=SNAP_ID,
        policy_version="pol-1",
        steps=(
            FetchMetricStep(step_id="s1", metric_keys=("metric.a",)),
            VerifyStep(
                step_id="s2",
                input_refs=("s1",),
                invariant_ids=("typed_result_present",),
                depends_on=("s1",),
            ),
        ),
    )


def _verify_record(plan: ExecutionPlan) -> PlanExecutionRecord:
    return PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(
            _step_receipt("s1", "fetch_metric", FETCH_DIGEST, HEX),
            _step_receipt("s2", "verify", VERIFY_DIGEST),
        ),
        output_step_ids=("s1", "s2"),
    )


def _verify_artifact() -> AnswerArtifact:
    return _artifact(step_id="s2", output_digest=VERIFY_DIGEST, rowset_sha256=None)


def _gateway_receipt(**o: Any) -> ExecutionReceipt:
    base: dict[str, Any] = {
        "datasource": "ds-1",
        "readonly_role": "agent_reader_user",
        "elapsed_ms": 4,
        "row_count": 10,
        "sql_fingerprint": "select-1",
        "policy_outcome": "allow",
        "authorization_revision": "auth-1",
        "rowset_sha256": HEX,
    }
    base.update(o)
    return ExecutionReceipt(**base)


def _fact(
    value: Any = 42,
    *,
    step_id: str = "s1",
    output_digest: str | None = FETCH_DIGEST,
    rowset_sha256: str | None = HEX,
) -> AnswerFact:
    return AnswerFact(
        fact_id=HEX,
        step_id=step_id,
        metric_key="metric.a",
        status="grounded",
        value=value,
        output_digest=output_digest,
        rowset_sha256=rowset_sha256,
    )


def _artifact(value: Any = 42, **o: Any) -> AnswerArtifact:
    return AnswerArtifact(facts=(_fact(value, **o),))


def _canonical(value: Any) -> Any:
    """Independent implementation of the frozen canonical fact spec."""

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite number")
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _facts_sha256(artifact: AnswerArtifact) -> str:
    payload = [_canonical(fact.model_dump(mode="python")) for fact in artifact.facts]
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _instant_artifact(moment: datetime) -> AnswerArtifact:
    fact = AnswerFact(
        fact_id=HEX,
        step_id="s1",
        metric_key="metric.a",
        status="grounded",
        value=42,
        output_digest=FETCH_DIGEST,
        rowset_sha256=HEX,
        data_as_of=moment,
    )
    return AnswerArtifact(facts=(fact,))


def _oracle(
    terminal: str, oracle_kind: str = "independent_calculation", **o: Any
) -> P4QOracleEvidence:
    base: dict[str, Any] = {
        "oracle_kind": oracle_kind,
        "oracle_revision": "oracle-1",
        "expected_terminal": terminal,
        "evidence_sha256": HEX,
        "independent_of_system_under_test": True,
    }
    if terminal == "answered":
        base["expected_answer_sha256"] = HEX
        base["expected_facts_sha256"] = _facts_sha256(_artifact())
    if oracle_kind == "independent_reference_sql":
        base["reference_sql_fingerprint"] = HEX
    base.update(o)
    return P4QOracleEvidence(**base)


def _registry(cases: tuple[P4QCaseSpec, ...] | None = None) -> P4QCaseRegistry:
    if cases is None:
        cases = tuple(
            P4QCaseSpec(
                case_id=f"case-{kind}",
                kind=kind,
                question=f"question for {kind}",
                expected_terminal=KIND_TERMINAL[kind],
            )
            for kind in KINDS
        )
    return P4QCaseRegistry(registry_revision="reg-1", cases=cases)


def _answered(case_id: str, kind: str, origin: str = "fixture", **o: Any) -> P4QCaseEvidence:
    trusted = kind == "approved_compute"
    plan = _plan(trusted=trusted)
    record = _record(plan, trusted=trusted)
    if trusted:
        artifact = _artifact(step_id="s2", output_digest=CALC_DIGEST, rowset_sha256=None)
    else:
        artifact = _artifact()
    facts_sha = _facts_sha256(artifact)
    base: dict[str, Any] = {
        "case_id": case_id,
        "evidence_origin": origin,
        "implementation_revision": "impl-1",
        "observed_terminal": "answered",
        "observed_mode": "QUERY",
        "codeact_used": False,
        "authorization_outcome": "allow",
        "hitl_pause_observed": kind == "clarification_resume",
        "hitl_resume_observed": kind == "clarification_resume",
        "pre_resume_sql_executions": 0,
        "sql_execution_count": 1,
        "execution_plan": plan,
        "execution_record": record,
        "answer_artifact": artifact,
        "observed_answer_sha256": HEX,
        "observed_facts_sha256": facts_sha,
        "oracle": _oracle("answered", expected_facts_sha256=facts_sha),
    }
    if kind in ("published_gold", "approved_compute"):
        base["gateway_receipt"] = _gateway_receipt()
    if kind == "published_gold":
        base["published_read_outcome"] = _read_outcome()
    base.update(o)
    return P4QCaseEvidence(**base)


def _denied(case_id: str, origin: str = "fixture", **o: Any) -> P4QCaseEvidence:
    base: dict[str, Any] = {
        "case_id": case_id,
        "evidence_origin": origin,
        "implementation_revision": "impl-1",
        "observed_terminal": "denied",
        "observed_mode": "QUERY",
        "codeact_used": False,
        "authorization_outcome": "deny",
        "hitl_pause_observed": False,
        "hitl_resume_observed": False,
        "pre_resume_sql_executions": 0,
        "sql_execution_count": 0,
        "oracle": _oracle("denied"),
    }
    base.update(o)
    return P4QCaseEvidence(**base)


def _missing(case_id: str, origin: str = "fixture", **o: Any) -> P4QCaseEvidence:
    base: dict[str, Any] = {
        "case_id": case_id,
        "evidence_origin": origin,
        "implementation_revision": "impl-1",
        "observed_terminal": "missing",
        "observed_mode": "QUERY",
        "codeact_used": False,
        "authorization_outcome": "allow",
        "hitl_pause_observed": False,
        "hitl_resume_observed": False,
        "pre_resume_sql_executions": 0,
        "sql_execution_count": 0,
        "published_read_outcome": _read_outcome("missing", None),
        "oracle": _oracle("missing"),
    }
    base.update(o)
    return P4QCaseEvidence(**base)


def _unavailable(case_id: str, origin: str = "fixture", **o: Any) -> P4QCaseEvidence:
    base: dict[str, Any] = {
        "case_id": case_id,
        "evidence_origin": origin,
        "implementation_revision": "impl-1",
        "observed_terminal": "unavailable",
        "observed_mode": "QUERY",
        "codeact_used": False,
        "authorization_outcome": "missing",
        "hitl_pause_observed": False,
        "hitl_resume_observed": False,
        "pre_resume_sql_executions": 0,
        "sql_execution_count": 0,
        "oracle": _oracle("unavailable"),
    }
    base.update(o)
    return P4QCaseEvidence(**base)


def _evidence_all(origin: str = "fixture") -> tuple[P4QCaseEvidence, ...]:
    return (
        _answered("case-published_gold", "published_gold", origin),
        _answered("case-approved_compute", "approved_compute", origin),
        _answered("case-clarification_resume", "clarification_resume", origin),
        _denied("case-authorization_denied", origin),
        _missing("case-missing", origin),
        _unavailable("case-unavailable", origin),
    )


def _witness(**o: Any) -> P4QRealnessWitness:
    base: dict[str, Any] = {
        "implementation_revision": "impl-1",
        "backend_authorization_revision": "auth-1",
        "backend_authorization_evidence_sha256": HEX,
        "semantic_release_id": "release-1",
        "semantic_release_checksum": HEX,
        "published_source_id": "source-1",
        "active_binding_evidence_sha256": HEX,
        "datasource": "ds-1",
        "readonly_role": "agent_reader_user",
        "product_identity": "agent_reader_user",
        "query_gateway_evidence_sha256": HEX,
        "reference_oracle_bundle_sha256": HEX,
        "remote_business_db_evidence_sha256": HEX,
        "captured_at": AWARE,
    }
    base.update(o)
    return P4QRealnessWitness(**base)


def _gold_manifest(**o: Any):
    evidence = _evidence_all()
    gold = _answered("case-published_gold", "published_gold", **o)
    return evaluate_p4q(_registry(), (gold, *evidence[1:]))


def _gold_with_artifact(artifact: AnswerArtifact):
    sha = _facts_sha256(artifact)
    return _gold_manifest(
        answer_artifact=artifact,
        observed_facts_sha256=sha,
        oracle=_oracle("answered", expected_facts_sha256=sha),
    )


# --- registry ---


def test_registry_accepts_every_required_kind() -> None:
    assert [c.kind for c in _registry().cases] == list(KINDS)


def test_registry_rejects_a_missing_required_kind() -> None:
    with pytest.raises(ValidationError):
        _registry(_registry().cases[:-1])


def test_registry_rejects_duplicate_case_ids() -> None:
    extra = P4QCaseSpec(case_id="case-missing", kind="missing", question="q", expected_terminal="missing")
    with pytest.raises(ValidationError):
        _registry(_registry().cases + (extra,))


def test_registry_and_specs_are_frozen_strict_and_forbid_unknown_fields() -> None:
    registry = _registry()
    with pytest.raises(ValidationError):
        registry.registry_revision = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        P4QCaseSpec(case_id="x", kind="missing", question="q", expected_terminal="missing", extra=1)
    with pytest.raises(ValidationError):
        P4QCaseSpec(case_id="x", kind="bogus", question="q", expected_terminal="missing")


def test_case_kind_cannot_redefine_its_terminal() -> None:
    with pytest.raises(ValidationError):
        P4QCaseSpec(case_id="x", kind="published_gold", question="q", expected_terminal="denied")


def test_registry_order_is_retained_in_the_manifest() -> None:
    cases = tuple(reversed(_registry().cases))
    manifest = evaluate_p4q(_registry(cases), _evidence_all())
    assert [r.case_id for r in manifest.case_results] == [c.case_id for c in cases]


# --- manifest / case-result self-consistency ---


def _case_result(passed: bool = True, failures: tuple[str, ...] = ()) -> P4QCaseResult:
    return P4QCaseResult(case_id="c", kind="published_gold", passed=passed, failures=failures)


def _manifest(verdict: str, **o: Any) -> P4QManifest:
    base: dict[str, Any] = {
        "registry_revision": "r",
        "verdict": verdict,
        "case_results": (_case_result(),),
        "readiness_gaps": (),
        "realness_witness_present": False,
    }
    base.update(o)
    return P4QManifest(**base)


def test_manifest_rejects_pass_with_a_failed_case() -> None:
    with pytest.raises(ValidationError):
        _manifest(
            "P4-Q-PASS", case_results=(_case_result(False, ("x",)),), realness_witness_present=True
        )


def test_manifest_rejects_pass_with_a_readiness_gap() -> None:
    with pytest.raises(ValidationError):
        _manifest("P4-Q-PASS", readiness_gaps=("g",), realness_witness_present=True)


def test_manifest_rejects_pass_without_a_witness() -> None:
    with pytest.raises(ValidationError):
        _manifest("P4-Q-PASS", realness_witness_present=False)


def test_manifest_rejects_contract_ready_without_a_gap() -> None:
    with pytest.raises(ValidationError):
        _manifest("P4-Q-CONTRACT_READY")


def test_manifest_rejects_fail_without_a_failed_case_or_contradiction() -> None:
    with pytest.raises(ValidationError):
        _manifest("P4-Q-FAIL")


def test_manifest_accepts_fail_with_a_failed_case() -> None:
    assert _manifest("P4-Q-FAIL", case_results=(_case_result(False, ("x",)),)).verdict == "P4-Q-FAIL"


def test_case_result_rejects_passed_true_with_failures() -> None:
    with pytest.raises(ValidationError):
        P4QCaseResult(case_id="c", kind="published_gold", passed=True, failures=("x",))


def test_case_result_rejects_passed_false_without_failures() -> None:
    with pytest.raises(ValidationError):
        P4QCaseResult(case_id="c", kind="published_gold", passed=False, failures=())


# --- verdict ceiling / realness ---


def test_fixture_evidence_ceiling_is_contract_ready_never_pass() -> None:
    manifest = evaluate_p4q(_registry(), _evidence_all("fixture"))
    assert manifest.verdict == "P4-Q-CONTRACT_READY"
    assert all(r.passed for r in manifest.case_results)
    assert manifest.readiness_gaps == ("non_real_evidence_origin", "realness_witness_absent")


def test_local_integration_evidence_ceiling_is_contract_ready() -> None:
    assert evaluate_p4q(_registry(), _evidence_all("local_integration")).verdict == "P4-Q-CONTRACT_READY"


def test_current_environment_cannot_produce_pass_without_realness() -> None:
    for origin in ("fixture", "local_integration"):
        assert evaluate_p4q(_registry(), _evidence_all(origin)).verdict != "P4-Q-PASS"


def test_pass_branch_is_only_contract_representability_not_a_real_pass() -> None:
    # THIS IS CONTRACT REPRESENTABILITY ONLY.  IT IS NOT A REAL P4-Q PASS.
    manifest = evaluate_p4q(_registry(), _evidence_all("real"), realness_witness=_witness())
    assert manifest.verdict == "P4-Q-PASS"
    assert manifest.readiness_gaps == ()
    assert manifest.realness_witness_present is True


def test_real_evidence_without_witness_is_contract_ready() -> None:
    manifest = evaluate_p4q(_registry(), _evidence_all("real"))
    assert manifest.verdict == "P4-Q-CONTRACT_READY"
    assert manifest.readiness_gaps == ("realness_witness_absent",)


@pytest.mark.parametrize(
    "field,value",
    [
        ("implementation_revision", "impl-OTHER"),
        ("datasource", "ds-OTHER"),
        ("readonly_role", "role-OTHER"),
        ("backend_authorization_revision", "auth-OTHER"),
        ("semantic_release_id", "release-OTHER"),
        ("published_source_id", "source-OTHER"),
    ],
)
def test_realness_witness_contradiction_is_fail_not_contract_ready(field: str, value: str) -> None:
    manifest = evaluate_p4q(
        _registry(), _evidence_all("real"), realness_witness=_witness(**{field: value})
    )
    assert manifest.verdict == "P4-Q-FAIL"
    assert "realness_mismatch" in manifest.integrity_failures


# --- plan / record binding ---


def test_plan_record_checksum_mismatch_fails() -> None:
    plan_a = _plan(trusted=False)
    record_b = _record(_plan(trusted=True), trusted=True)
    manifest = _gold_manifest(execution_plan=plan_a, execution_record=record_b)
    assert manifest.verdict == "P4-Q-FAIL"
    assert "execution_plan_record_mismatch" in manifest.case_results[0].failures


def test_plan_record_exact_binding_is_valid() -> None:
    assert _gold_manifest().case_results[0].passed is True


# --- gateway / step receipt binding ---


def test_gateway_rowset_unbound_to_fetch_receipt_fails() -> None:
    manifest = _gold_manifest(gateway_receipt=_gateway_receipt(rowset_sha256=OTHER))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "gateway_execution_evidence_mismatch" in manifest.case_results[0].failures


def test_gateway_rowset_without_a_fetch_receipt_fails() -> None:
    plan = _plan(trusted=False)
    manifest = _gold_manifest(execution_plan=plan, execution_record=_verify_only_record(plan))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "gateway_execution_evidence_mismatch" in manifest.case_results[0].failures


def test_gateway_rowset_bound_to_fetch_receipt_is_valid() -> None:
    assert _gold_manifest().case_results[0].passed is True


# --- grounding / execution binding ---


def test_grounded_fact_step_unknown_to_receipts_fails() -> None:
    manifest = _gold_with_artifact(_artifact(step_id="s9"))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "grounding_execution_mismatch" in manifest.case_results[0].failures


def test_grounded_fact_output_digest_mismatch_fails() -> None:
    manifest = _gold_with_artifact(_artifact(output_digest=OTHER))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "grounding_execution_mismatch" in manifest.case_results[0].failures


def test_grounded_fact_rowset_mismatch_fails() -> None:
    manifest = _gold_with_artifact(_artifact(rowset_sha256=OTHER))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "grounding_execution_mismatch" in manifest.case_results[0].failures


# --- published gold ---


def test_published_gold_positive_fixture_passes_its_contract_checks() -> None:
    assert _gold_manifest().case_results[0].passed is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"published_read_outcome": None},
        {"published_read_outcome": _failed_read_outcome()},
        {"published_read_outcome": _read_outcome("missing", None)},
        {"published_read_outcome": _read_outcome("no_data", None)},
        {"gateway_receipt": None},
        {"gateway_receipt": _gateway_receipt(policy_outcome="deny")},
        {"gateway_receipt": _gateway_receipt(authorization_revision=None)},
        {"gateway_receipt": _gateway_receipt(rowset_sha256=None)},
        {"gateway_receipt": _gateway_receipt(sql_fingerprint="")},
        {"gateway_receipt": _gateway_receipt(row_count=0)},
        {"answer_artifact": None},
        {"answer_artifact": AnswerArtifact(facts=())},
        {"observed_facts_sha256": OTHER},
        {"observed_answer_sha256": OTHER},
    ],
)
def test_published_gold_fails_when_a_load_bearing_element_is_missing(
    overrides: dict[str, Any],
) -> None:
    manifest = _gold_manifest(**overrides)
    assert manifest.verdict == "P4-Q-FAIL"
    assert manifest.case_results[0].passed is False


def test_published_gold_does_not_infer_a_p3_outcome_from_sql_evidence() -> None:
    manifest = _gold_manifest(published_read_outcome=None)
    assert "published_read_missing" in manifest.case_results[0].failures


def test_published_gold_rejects_a_non_answer_p3_result() -> None:
    manifest = _gold_manifest(published_read_outcome=_read_outcome("missing", None))
    assert "published_read_failed" in manifest.case_results[0].failures


def test_published_gold_does_not_require_a_metric_code_mapping() -> None:
    assert _gold_manifest().case_results[0].passed is True


# --- A1: grounding mismatch consumer ---


def test_grounding_mismatch_degradation_flag_fails_the_case() -> None:
    """The grounding producer flag must reach the acceptance verdict.

    The agent never gets a fact in this situation (grounding fails closed), so
    the flag is the ONLY machine-readable signal that a real re-binding
    mismatch occurred.
    """
    artifact = AnswerArtifact(facts=(), degradation_flags=("grounding_execution_mismatch",))
    manifest = _gold_with_artifact(artifact)
    assert manifest.verdict == "P4-Q-FAIL"
    assert "grounding_execution_mismatch" in manifest.case_results[0].failures


def test_ordinary_degradation_flags_do_not_fail_the_case() -> None:
    # stale / unknown-freshness / source degradation are evidence, not mismatch
    for flag in (
        "GroundedAnswerStale",
        "GroundedAnswerFreshnessUnknown",
        "weak_light_source_degraded",
    ):
        artifact = _artifact()
        flagged = AnswerArtifact(facts=artifact.facts, degradation_flags=(flag,))
        manifest = _gold_with_artifact(flagged)
        assert "grounding_execution_mismatch" not in manifest.case_results[0].failures, flag


def test_clean_artifact_has_no_grounding_mismatch_failure() -> None:
    manifest = _gold_with_artifact(_artifact())
    assert "grounding_execution_mismatch" not in manifest.case_results[0].failures


# --- approved compute ---


def test_approved_compute_with_trusted_step_passes() -> None:
    assert evaluate_p4q(_registry(), _evidence_all()).case_results[1].passed is True


def test_fetch_only_plan_cannot_satisfy_approved_compute() -> None:
    evidence = _evidence_all()
    plan = _plan(trusted=False)
    fetch_only = _answered(
        "case-approved_compute",
        "approved_compute",
        execution_plan=plan,
        execution_record=_record(plan, trusted=False),
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], fetch_only, *evidence[2:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "trusted_calculation_missing" in manifest.case_results[1].failures


def test_approved_compute_requires_a_calculation_grounded_fact() -> None:
    evidence = _evidence_all()
    fetch_artifact = _artifact()
    sha = _facts_sha256(fetch_artifact)
    fetch_grounded = _answered(
        "case-approved_compute",
        "approved_compute",
        answer_artifact=fetch_artifact,
        observed_facts_sha256=sha,
        oracle=_oracle("answered", expected_facts_sha256=sha),
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], fetch_grounded, *evidence[2:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "trusted_calculation_grounding_mismatch" in manifest.case_results[1].failures


def test_approved_compute_with_calculation_grounded_fact_is_valid() -> None:
    assert evaluate_p4q(_registry(), _evidence_all()).case_results[1].passed is True


# --- clarification ---


def test_clarification_resume_with_zero_pre_resume_sql_passes() -> None:
    assert evaluate_p4q(_registry(), _evidence_all()).case_results[2].passed is True


def test_clarification_requires_post_resume_execution_evidence() -> None:
    evidence = _evidence_all()
    bad = _answered(
        "case-clarification_resume",
        "clarification_resume",
        execution_plan=None,
        execution_record=None,
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], bad, *evidence[3:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "execution_missing" in manifest.case_results[2].failures


def test_clarification_record_must_bind_to_the_supplied_plan() -> None:
    evidence = _evidence_all()
    other_plan = _plan(trusted=True)
    bad = _answered(
        "case-clarification_resume",
        "clarification_resume",
        execution_record=_record(other_plan, trusted=True),
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], bad, *evidence[3:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "execution_plan_record_mismatch" in manifest.case_results[2].failures


def test_clarification_resume_with_pre_resume_sql_fails() -> None:
    evidence = _evidence_all()
    bad = _answered("case-clarification_resume", "clarification_resume", pre_resume_sql_executions=1)
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], bad, *evidence[3:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "execution_before_resume" in manifest.case_results[2].failures


def test_clarification_resume_requires_hitl_pause_and_resume() -> None:
    evidence = _evidence_all()
    bad = _answered("case-clarification_resume", "clarification_resume", hitl_resume_observed=False)
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], bad, *evidence[3:]))
    assert manifest.verdict == "P4-Q-FAIL"


# --- authorization denial ---


def test_authorization_denial_with_zero_sql_passes() -> None:
    assert evaluate_p4q(_registry(), _evidence_all()).case_results[3].passed is True


def test_authorization_denial_with_sql_execution_fails() -> None:
    evidence = _evidence_all()
    bad = _denied("case-authorization_denied", sql_execution_count=1)
    manifest = evaluate_p4q(_registry(), (*evidence[:3], bad, *evidence[4:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_sql_execution" in manifest.case_results[3].failures


def test_authorization_denial_with_gateway_receipt_fails() -> None:
    evidence = _evidence_all()
    bad = _denied("case-authorization_denied", gateway_receipt=_gateway_receipt())
    manifest = evaluate_p4q(_registry(), (*evidence[:3], bad, *evidence[4:]))
    assert manifest.verdict == "P4-Q-FAIL"


def test_authorization_denial_with_successful_execution_record_fails() -> None:
    evidence = _evidence_all()
    plan = _plan(trusted=False)
    bad = _denied("case-authorization_denied", execution_plan=plan, execution_record=_record(plan))
    manifest = evaluate_p4q(_registry(), (*evidence[:3], bad, *evidence[4:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_execution_evidence" in manifest.case_results[3].failures


def test_authorization_denial_with_a_successful_step_receipt_fails() -> None:
    evidence = _evidence_all()
    plan = _plan(trusted=False)
    bad = _denied(
        "case-authorization_denied",
        execution_plan=plan,
        execution_record=_failed_record_with_succeeded_step(plan),
    )
    manifest = evaluate_p4q(_registry(), (*evidence[:3], bad, *evidence[4:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_execution_evidence" in manifest.case_results[3].failures


# --- unavailable ---


def test_unavailable_is_distinct_from_missing() -> None:
    assert evaluate_p4q(_registry(), _evidence_all()).case_results[5].passed is True


def test_unavailable_rejects_synthesized_missing() -> None:
    evidence = _evidence_all()
    bad = _unavailable("case-unavailable", published_read_outcome=_read_outcome("missing", None))
    manifest = evaluate_p4q(_registry(), (*evidence[:5], bad))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "synthesized_missing" in manifest.case_results[5].failures


def test_unavailable_rejects_grounded_business_facts() -> None:
    evidence = _evidence_all()
    bad = _unavailable("case-unavailable", answer_artifact=_artifact())
    manifest = evaluate_p4q(_registry(), (*evidence[:5], bad))
    assert manifest.verdict == "P4-Q-FAIL"


def test_unavailable_rejects_successful_execution() -> None:
    evidence = _evidence_all()
    plan = _plan(trusted=False)
    bad = _unavailable("case-unavailable", execution_plan=plan, execution_record=_record(plan))
    manifest = evaluate_p4q(_registry(), (*evidence[:5], bad))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_execution_evidence" in manifest.case_results[5].failures


def test_unavailable_rejects_successful_gateway_receipt() -> None:
    evidence = _evidence_all()
    bad = _unavailable("case-unavailable", gateway_receipt=_gateway_receipt())
    manifest = evaluate_p4q(_registry(), (*evidence[:5], bad))
    assert manifest.verdict == "P4-Q-FAIL"


# --- missing ---


def test_business_missing_is_not_a_reader_failure() -> None:
    assert evaluate_p4q(_registry(), _evidence_all()).case_results[4].passed is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"published_read_outcome": _read_outcome("success", Decimal("0"))},
        {"published_read_outcome": _failed_read_outcome("source_unavailable")},
        {"published_read_outcome": None},
    ],
)
def test_missing_case_rejects_zero_and_reader_failure(overrides: dict[str, Any]) -> None:
    evidence = _evidence_all()
    bad = _missing("case-missing", **overrides)
    manifest = evaluate_p4q(_registry(), (*evidence[:4], bad, *evidence[5:]))
    assert manifest.verdict == "P4-Q-FAIL"


# --- mode / codeact ---


@pytest.mark.parametrize("mode", ["ANALYZE", "BUILD"])
def test_mode_escalation_fails_on_every_path(mode: str) -> None:
    evidence = _evidence_all()
    gold = _answered("case-published_gold", "published_gold", observed_mode=mode)
    manifest = evaluate_p4q(_registry(), (gold, *evidence[1:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "mode_escalation" in manifest.case_results[0].failures


def test_codeact_fails_even_on_approved_compute() -> None:
    evidence = _evidence_all()
    bad = _answered("case-approved_compute", "approved_compute", codeact_used=True)
    manifest = evaluate_p4q(_registry(), (evidence[0], bad, *evidence[2:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "codeact_used" in manifest.case_results[1].failures


def test_security_flags_reject_non_boolean_values() -> None:
    with pytest.raises(ValidationError):
        _answered("case-published_gold", "published_gold", codeact_used=1)
    with pytest.raises(ValidationError):
        _answered("case-published_gold", "published_gold", hitl_resume_observed="true")
    with pytest.raises(ValidationError):
        _oracle("answered", independent_of_system_under_test=1)


# --- grounding / oracle ---


def test_artifact_derived_fact_digest_mismatch_fails() -> None:
    manifest = _gold_manifest(
        observed_facts_sha256=OTHER, oracle=_oracle("answered", expected_facts_sha256=OTHER)
    )
    assert manifest.verdict == "P4-Q-FAIL"
    assert "facts_digest_mismatch" in manifest.case_results[0].failures


def test_oracle_expected_fact_digest_mismatch_fails() -> None:
    manifest = _gold_manifest(oracle=_oracle("answered", expected_facts_sha256=OTHER))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "facts_digest_mismatch" in manifest.case_results[0].failures


def test_answer_digest_mismatch_fails() -> None:
    manifest = _gold_manifest(observed_answer_sha256=OTHER)
    assert manifest.verdict == "P4-Q-FAIL"
    assert "answer_digest_mismatch" in manifest.case_results[0].failures


def test_answer_grounded_without_a_value_fails() -> None:
    fact = AnswerFact(
        fact_id=HEX,
        step_id="s1",
        metric_key="metric.a",
        status="unavailable",
        value=None,
        output_digest=FETCH_DIGEST,
        rowset_sha256=HEX,
    )
    manifest = _gold_with_artifact(AnswerArtifact(facts=(fact,)))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "grounded_answer_missing" in manifest.case_results[0].failures


def test_system_generated_oracle_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _oracle("answered", independent_of_system_under_test=False)


def test_reference_sql_oracle_requires_a_fingerprint() -> None:
    with pytest.raises(ValidationError):
        P4QOracleEvidence(
            oracle_kind="independent_reference_sql",
            oracle_revision="o",
            expected_terminal="missing",
            evidence_sha256=HEX,
            independent_of_system_under_test=True,
        )


def test_answered_oracle_requires_both_digests() -> None:
    with pytest.raises(ValidationError):
        P4QOracleEvidence(
            oracle_kind="approved_fact",
            oracle_revision="o",
            expected_terminal="answered",
            evidence_sha256=HEX,
            independent_of_system_under_test=True,
        )


# --- evidence set integrity ---


def test_duplicate_evidence_is_rejected_not_last_write_wins() -> None:
    evidence = _evidence_all()
    manifest = evaluate_p4q(_registry(), (*evidence, evidence[0]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "duplicate_case_evidence" in manifest.integrity_failures


def test_unknown_evidence_is_rejected_not_silently_ignored() -> None:
    evidence = _evidence_all()
    stray = _missing("case-not-in-registry")
    manifest = evaluate_p4q(_registry(), (*evidence, stray))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unknown_case_evidence" in manifest.integrity_failures


def test_missing_registry_evidence_fails() -> None:
    evidence = _evidence_all()
    manifest = evaluate_p4q(_registry(), evidence[:-1])
    assert manifest.verdict == "P4-Q-FAIL"
    assert "case_evidence_missing" in manifest.case_results[5].failures


# --- Iteration-3: missing/unavailable + rogue receipts ---


def test_missing_rejects_sql_execution() -> None:
    evidence = _evidence_all()
    bad = _missing("case-missing", sql_execution_count=1)
    manifest = evaluate_p4q(_registry(), (*evidence[:4], bad, *evidence[5:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_execution_evidence" in manifest.case_results[4].failures


def test_missing_rejects_a_failed_record_with_a_successful_step() -> None:
    evidence = _evidence_all()
    plan = _plan(trusted=False)
    bad = _missing(
        "case-missing",
        execution_plan=plan,
        execution_record=_failed_record_with_succeeded_step(plan),
    )
    manifest = evaluate_p4q(_registry(), (*evidence[:4], bad, *evidence[5:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_execution_evidence" in manifest.case_results[4].failures


def test_missing_rejects_a_gateway_receipt() -> None:
    evidence = _evidence_all()
    bad = _missing("case-missing", gateway_receipt=_gateway_receipt())
    manifest = evaluate_p4q(_registry(), (*evidence[:4], bad, *evidence[5:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_execution_evidence" in manifest.case_results[4].failures


def test_unavailable_rejects_sql_execution() -> None:
    evidence = _evidence_all()
    bad = _unavailable("case-unavailable", sql_execution_count=1)
    manifest = evaluate_p4q(_registry(), (*evidence[:5], bad))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_execution_evidence" in manifest.case_results[5].failures


def test_unavailable_rejects_a_failed_record_with_a_successful_step() -> None:
    evidence = _evidence_all()
    plan = _plan(trusted=False)
    bad = _unavailable(
        "case-unavailable",
        execution_plan=plan,
        execution_record=_failed_record_with_succeeded_step(plan),
        sql_execution_count=1,
    )
    manifest = evaluate_p4q(_registry(), (*evidence[:5], bad))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_execution_evidence" in manifest.case_results[5].failures


def test_unavailable_rejects_a_successful_published_read() -> None:
    evidence = _evidence_all()
    bad = _unavailable("case-unavailable", published_read_outcome=_read_outcome())
    manifest = evaluate_p4q(_registry(), (*evidence[:5], bad))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_execution_evidence" in manifest.case_results[5].failures


def test_unavailable_accepts_a_failed_published_read() -> None:
    evidence = _evidence_all()
    ok = _unavailable("case-unavailable", published_read_outcome=_failed_read_outcome())
    manifest = evaluate_p4q(_registry(), (*evidence[:5], ok))
    assert manifest.case_results[5].passed is True


def test_record_receipt_step_absent_from_plan_fails() -> None:
    plan = _plan(trusted=False)
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(_step_receipt("rogue", "fetch_metric", FETCH_DIGEST, HEX),),
        output_step_ids=("rogue",),
    )
    manifest = _gold_manifest(execution_plan=plan, execution_record=record)
    assert manifest.verdict == "P4-Q-FAIL"
    assert "execution_plan_record_mismatch" in manifest.case_results[0].failures


def test_record_receipt_kind_mismatch_fails() -> None:
    plan = _plan(trusted=False)
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(_step_receipt("s1", "verify", FETCH_DIGEST, HEX),),
        output_step_ids=("s1",),
    )
    manifest = _gold_manifest(execution_plan=plan, execution_record=record)
    assert manifest.verdict == "P4-Q-FAIL"
    assert "execution_plan_record_mismatch" in manifest.case_results[0].failures


def test_rogue_fetch_receipt_cannot_bind_gateway() -> None:
    plan = _plan(trusted=False)
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(_step_receipt("rogue", "fetch_metric", FETCH_DIGEST, HEX),),
        output_step_ids=("rogue",),
    )
    manifest = _gold_manifest(execution_plan=plan, execution_record=record)
    assert manifest.verdict == "P4-Q-FAIL"
    assert "gateway_execution_evidence_mismatch" in manifest.case_results[0].failures


def test_rogue_trusted_calculation_receipt_cannot_satisfy_approved_compute() -> None:
    evidence = _evidence_all()
    plan = _plan(trusted=False)
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(
            _step_receipt("s1", "fetch_metric", FETCH_DIGEST, HEX),
            _step_receipt("rogue", "trusted_calculation", CALC_DIGEST),
        ),
        output_step_ids=("s1", "rogue"),
    )
    bad = _answered(
        "case-approved_compute",
        "approved_compute",
        execution_plan=plan,
        execution_record=record,
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], bad, *evidence[2:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "execution_plan_record_mismatch" in manifest.case_results[1].failures


def test_rogue_verify_receipt_fails() -> None:
    plan = _plan(trusted=False)
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(_step_receipt("rogue_verify", "verify", FETCH_DIGEST),),
        output_step_ids=("rogue_verify",),
    )
    manifest = _gold_manifest(execution_plan=plan, execution_record=record)
    assert manifest.verdict == "P4-Q-FAIL"
    assert "execution_plan_record_mismatch" in manifest.case_results[0].failures


def test_succeeded_record_missing_a_planned_step_fails() -> None:
    plan = _plan(trusted=True)
    record = _record(plan, trusted=False)
    manifest = _gold_manifest(execution_plan=plan, execution_record=record)
    assert manifest.verdict == "P4-Q-FAIL"
    assert "execution_plan_record_mismatch" in manifest.case_results[0].failures


def test_failed_record_with_a_valid_plan_prefix_is_not_a_structural_failure() -> None:
    plan = _plan(trusted=True)
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="failed",
        step_receipts=(_step_receipt("s1", "fetch_metric", FETCH_DIGEST, HEX),),
        output_step_ids=("s1",),
        stop_reason="boom",
    )
    evidence = _evidence_all()
    bad = _unavailable("case-unavailable", execution_plan=plan, execution_record=record)
    manifest = evaluate_p4q(_registry(), (*evidence[:5], bad))
    assert "execution_plan_record_mismatch" not in manifest.case_results[5].failures


def test_manifest_rejects_an_empty_case_set_for_pass() -> None:
    with pytest.raises(ValidationError):
        P4QManifest(
            registry_revision="r",
            verdict="P4-Q-PASS",
            case_results=(),
            readiness_gaps=(),
            realness_witness_present=True,
        )


def test_manifest_rejects_an_empty_case_set_for_contract_ready() -> None:
    with pytest.raises(ValidationError):
        P4QManifest(
            registry_revision="r",
            verdict="P4-Q-CONTRACT_READY",
            case_results=(),
            readiness_gaps=("g",),
            realness_witness_present=False,
        )


# --- Iteration-4: missing grounded facts + optional receipt validation ---


def test_missing_rejects_a_grounded_business_fact() -> None:
    evidence = _evidence_all()
    bad = _missing("case-missing", answer_artifact=_artifact(value=42))
    manifest = evaluate_p4q(_registry(), (*evidence[:4], bad, *evidence[5:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "unexpected_grounded_facts" in manifest.case_results[4].failures


def test_missing_without_a_grounded_value_remains_valid() -> None:
    evidence = _evidence_all()
    ok = _missing("case-missing", answer_artifact=AnswerArtifact(facts=()))
    manifest = evaluate_p4q(_registry(), (*evidence[:4], ok, *evidence[5:]))
    assert manifest.case_results[4].passed is True


def test_clarification_rejects_a_denied_gateway_receipt() -> None:
    evidence = _evidence_all()
    bad = _answered(
        "case-clarification_resume",
        "clarification_resume",
        gateway_receipt=_gateway_receipt(policy_outcome="deny"),
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], bad, *evidence[3:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "gateway_receipt_invalid" in manifest.case_results[2].failures


def test_clarification_rejects_an_unbound_gateway_rowset() -> None:
    evidence = _evidence_all()
    bad = _answered(
        "case-clarification_resume",
        "clarification_resume",
        gateway_receipt=_gateway_receipt(rowset_sha256=OTHER),
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], bad, *evidence[3:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "gateway_execution_evidence_mismatch" in manifest.case_results[2].failures


def test_clarification_without_a_gateway_receipt_remains_valid() -> None:
    assert evaluate_p4q(_registry(), _evidence_all()).case_results[2].passed is True


def test_clarification_with_a_valid_bound_gateway_receipt_is_valid() -> None:
    evidence = _evidence_all()
    ok = _answered(
        "case-clarification_resume",
        "clarification_resume",
        gateway_receipt=_gateway_receipt(),
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], ok, *evidence[3:]))
    assert manifest.case_results[2].passed is True


def test_published_gold_rejects_a_verify_grounded_answer() -> None:
    plan = _verify_plan()
    artifact = _verify_artifact()
    sha = _facts_sha256(artifact)
    manifest = _gold_manifest(
        execution_plan=plan,
        execution_record=_verify_record(plan),
        answer_artifact=artifact,
        observed_facts_sha256=sha,
        oracle=_oracle("answered", expected_facts_sha256=sha),
    )
    assert manifest.verdict == "P4-Q-FAIL"
    assert "grounding_execution_mismatch" in manifest.case_results[0].failures


def test_clarification_rejects_a_verify_grounded_answer() -> None:
    evidence = _evidence_all()
    plan = _verify_plan()
    artifact = _verify_artifact()
    sha = _facts_sha256(artifact)
    bad = _answered(
        "case-clarification_resume",
        "clarification_resume",
        execution_plan=plan,
        execution_record=_verify_record(plan),
        answer_artifact=artifact,
        observed_facts_sha256=sha,
        oracle=_oracle("answered", expected_facts_sha256=sha),
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], bad, *evidence[3:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "grounding_execution_mismatch" in manifest.case_results[2].failures


def test_approved_compute_rejects_a_verify_grounded_answer() -> None:
    evidence = _evidence_all()
    plan = _verify_plan()
    artifact = _verify_artifact()
    sha = _facts_sha256(artifact)
    bad = _answered(
        "case-approved_compute",
        "approved_compute",
        execution_plan=plan,
        execution_record=_verify_record(plan),
        answer_artifact=artifact,
        observed_facts_sha256=sha,
        oracle=_oracle("answered", expected_facts_sha256=sha),
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], bad, *evidence[2:]))
    assert manifest.verdict == "P4-Q-FAIL"


# --- Iteration-5: SQL-count lower bound, fact canonicalization, one revision ---


def test_published_gold_requires_a_positive_sql_execution_count() -> None:
    manifest = _gold_manifest(sql_execution_count=0)
    assert manifest.verdict == "P4-Q-FAIL"
    assert "sql_execution_count_mismatch" in manifest.case_results[0].failures


def test_approved_compute_requires_a_positive_sql_execution_count() -> None:
    evidence = _evidence_all()
    bad = _answered("case-approved_compute", "approved_compute", sql_execution_count=0)
    manifest = evaluate_p4q(_registry(), (evidence[0], bad, *evidence[2:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "sql_execution_count_mismatch" in manifest.case_results[1].failures


def test_clarification_with_a_gateway_requires_a_positive_sql_execution_count() -> None:
    evidence = _evidence_all()
    bad = _answered(
        "case-clarification_resume",
        "clarification_resume",
        gateway_receipt=_gateway_receipt(),
        sql_execution_count=0,
    )
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], bad, *evidence[3:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "sql_execution_count_mismatch" in manifest.case_results[2].failures


def test_clarification_with_fetch_evidence_requires_a_positive_sql_execution_count() -> None:
    evidence = _evidence_all()
    bad = _answered("case-clarification_resume", "clarification_resume", sql_execution_count=0)
    manifest = evaluate_p4q(_registry(), (evidence[0], evidence[1], bad, *evidence[3:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "sql_execution_count_mismatch" in manifest.case_results[2].failures


def test_positive_path_allows_more_than_one_sql_execution() -> None:
    evidence = _evidence_all()
    ok = _answered("case-published_gold", "published_gold", sql_execution_count=3)
    manifest = evaluate_p4q(_registry(), (ok, *evidence[1:]))
    assert manifest.case_results[0].passed is True


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), {"x": [1, float("nan")]}],
)
def test_non_finite_fact_values_fail_closed(value: Any) -> None:
    evidence = _evidence_all()
    bad = _answered(
        "case-published_gold", "published_gold", answer_artifact=_artifact(value=value)
    )
    manifest = evaluate_p4q(_registry(), (bad, *evidence[1:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "facts_digest_invalid" in manifest.case_results[0].failures


def test_naive_data_as_of_fails_closed() -> None:
    evidence = _evidence_all()
    naive_fact = AnswerFact(
        fact_id=HEX,
        step_id="s1",
        metric_key="metric.a",
        status="grounded",
        value=42,
        output_digest=FETCH_DIGEST,
        rowset_sha256=HEX,
        data_as_of=datetime(2026, 1, 2, 3, 4, 5),
    )
    bad = _answered(
        "case-published_gold", "published_gold", answer_artifact=AnswerArtifact(facts=(naive_fact,))
    )
    manifest = evaluate_p4q(_registry(), (bad, *evidence[1:]))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "facts_digest_invalid" in manifest.case_results[0].failures


def test_equivalent_instants_produce_the_same_fact_digest() -> None:
    utc = _instant_artifact(datetime(2026, 1, 2, 3, 0, tzinfo=timezone.utc))
    plus_eight = _instant_artifact(
        datetime(2026, 1, 2, 11, 0, tzinfo=timezone(timedelta(hours=8)))
    )
    assert _facts_sha256(utc) == _facts_sha256(plus_eight)


def test_aware_datetime_fact_is_accepted_with_its_oracle_digest() -> None:
    evidence = _evidence_all()
    artifact = _instant_artifact(AWARE)
    sha = _facts_sha256(artifact)
    ok = _answered(
        "case-published_gold",
        "published_gold",
        answer_artifact=artifact,
        observed_facts_sha256=sha,
        oracle=_oracle("answered", expected_facts_sha256=sha),
    )
    manifest = evaluate_p4q(_registry(), (ok, *evidence[1:]))
    assert manifest.case_results[0].passed is True


@pytest.mark.parametrize("origin", ["fixture", "local_integration"])
def test_mixed_implementation_revisions_fail_without_a_witness(origin: str) -> None:
    evidence = list(_evidence_all(origin))
    evidence[3] = _denied(
        "case-authorization_denied", origin, implementation_revision="impl-B"
    )
    manifest = evaluate_p4q(_registry(), tuple(evidence))
    assert manifest.verdict == "P4-Q-FAIL"
    assert "implementation_revision_mismatch" in manifest.integrity_failures


def test_single_implementation_revision_preserves_the_fixture_ceiling() -> None:
    manifest = evaluate_p4q(_registry(), _evidence_all("fixture"))
    assert manifest.verdict == "P4-Q-CONTRACT_READY"
    assert manifest.implementation_revision == "impl-1"
    assert manifest.integrity_failures == ()


# --- determinism ---


def _subprocess_manifest(hashseed: str) -> str:
    code = (
        "import sys;sys.path.insert(0,'tests/unit');"
        "import test_p4q_acceptance as t;"
        "from src.nl2sql.orchestration.p4q_acceptance import evaluate_p4q;"
        "m=evaluate_p4q(t._registry(), t._evidence_all());"
        "print(m.verdict);print([c.case_id for c in m.case_results]);"
        "print([list(c.failures) for c in m.case_results]);print(list(m.readiness_gaps))"
    )
    env = dict(os.environ, PYTHONHASHSEED=hashseed)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_evaluation_is_deterministic_across_hash_seeds() -> None:
    in_process = evaluate_p4q(_registry(), _evidence_all())
    seeds = {_subprocess_manifest(seed) for seed in ("0", "1")}
    assert len(seeds) == 1
    assert in_process.verdict in seeds.pop()
