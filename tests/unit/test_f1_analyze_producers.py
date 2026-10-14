"""Slice F1 execution-level tests: ANALYZE evidence, cross-round budget, decisions.

Every test drives the real producer/validator and observes the result.  The
causal test additionally carries an executable PRE-FIX control: with the causal
gate disabled the very same statement is accepted, so the rejection is proven to
come from the new gate and not from a blanket statement ban.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

import src.nl2sql.orchestration.analysis_evidence as analysis_evidence
from src.nl2sql.contracts import (
    AnswerArtifact,
    AnswerFact,
    ContextBundle,
    PlanExecutionRecord,
    PlanStepReceipt,
    QueryPlan,
    TimeRange,
)
from src.nl2sql.orchestration.analysis_evidence import (
    AnalysisCausalSupport,
    AnalysisEvidenceError,
    AnalysisEvidenceFact,
    AnalysisInterpretation,
    AnalysisManualOriginEvidence,
    AnalysisScopeProvenance,
    AnalysisStatement,
    build_analysis_evidence,
    project_analysis_model_input,
    validate_analysis_interpretation,
)
from src.nl2sql.orchestration.budget import (
    BudgetExceeded,
    DrilldownBudgetLedger,
    DrilldownBudgetPolicy,
    DrilldownBudgetRecord,
    RouteBudgetLedger,
)
from src.nl2sql.orchestration.decision_contract import (
    DecisionContractError,
    HITLDecision,
    business_confirmation_request,
    resume_token,
    revalidate_request,
    risk_policy_decision_request,
)
from src.nl2sql.orchestration.grounding import GroundedAnswer

_CAUSAL_SENTENCE = "促销活动导致了投诉量下降，门店人力不足是根本原因。"

_STEP_ID = "fetch_complaints"
_METRIC_KEY = "complaint_count_overall_day"
_OUTPUT_DIGEST = "a" * 64
_ROWSET_SHA256 = "b" * 64
_SEMANTIC_SIGNATURE = "c" * 64
_PLAN_CHECKSUM = "d" * 64


def _fact_id(
    step_id: str,
    metric_key: str,
    status: str,
    value: object,
    time_range: TimeRange | None = None,
) -> str:
    payload: dict[str, object] = {
        "step_id": step_id,
        "metric_key": metric_key,
        "status": status,
        "value": value,
    }
    if time_range is not None:
        payload["time_range"] = time_range.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _bundle(
    *,
    value: object = 42,
    data_as_of: datetime | None = datetime(2026, 9, 20, tzinfo=UTC),
    time_range: TimeRange | None = None,
    scope_provenance: AnalysisScopeProvenance | None = None,
    manual_origin_evidence: tuple[AnalysisManualOriginEvidence, ...] = (),
    causal_support: tuple[AnalysisCausalSupport, ...] = (),
):
    receipt = PlanStepReceipt(
        step_id=_STEP_ID,
        kind="fetch_metric",
        status="succeeded",
        elapsed_ms=3,
        output_digest=_OUTPUT_DIGEST,
        rowset_sha256=_ROWSET_SHA256,
        data_as_of=data_as_of,
        freshness_status="fresh" if data_as_of else "unknown",
        source_kind="approved_aggregate",
        source_id="gold.complaint.archive",
        selection_reason="fresh_approved_aggregate",
        source_checkpoint="gold-checkpoint-20260920",
        semantic_signature=_SEMANTIC_SIGNATURE,
    )
    status = "unavailable" if value is None else "grounded"
    fact = AnswerFact(
        fact_id=_fact_id(_STEP_ID, _METRIC_KEY, status, value, time_range),
        step_id=_STEP_ID,
        metric_key=_METRIC_KEY,
        status=status,
        value=value,
        unit="count",
        time_range=time_range,
        rowset_sha256=receipt.rowset_sha256,
        output_digest=receipt.output_digest,
        source_id=receipt.source_id,
        semantic_signature=receipt.semantic_signature,
        data_as_of=data_as_of,
        freshness_status=receipt.freshness_status,
        source_kind=receipt.source_kind,
        selection_reason=receipt.selection_reason,
    )
    grounded = GroundedAnswer(
        answer_text="controlled answer",
        facts=(fact,),
        artifact=AnswerArtifact(facts=(fact,)),
    )
    record = PlanExecutionRecord(
        execution_plan_checksum=_PLAN_CHECKSUM,
        status="succeeded",
        step_receipts=(receipt,),
        output_step_ids=(_STEP_ID,),
    )
    window = TimeRange(start=date(2026, 9, 1), end=date(2026, 9, 20))
    return build_analysis_evidence(
        grounded=grounded,
        record=record,
        execution_plan_checksum=record.execution_plan_checksum,
        analysis_window=window,
        scope_provenance=scope_provenance,
        manual_origin_evidence=manual_origin_evidence,
        causal_support=causal_support,
    )


# --- 1. causal out-of-bounds gate --------------------------------------------


def test_causal_claim_without_evidence_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = _bundle()
    causal = AnalysisInterpretation(
        summary=AnalysisStatement(text=_CAUSAL_SENTENCE)
    )

    # PRE-FIX CONTROL.  The original validator only enforced numeric
    # traceability; the sentence carries no number, so it was ACCEPTED.  Here the
    # new causal gate is disabled to reproduce exactly that pre-fix outcome.
    monkeypatch.setattr(analysis_evidence, "_causal_markers", lambda _text: ())
    assert validate_analysis_interpretation(causal, evidence=bundle) is causal
    monkeypatch.undo()

    # FIXED: the same sentence is refused with a stable code.
    with pytest.raises(
        AnalysisEvidenceError,
        match="analysis_interpretation_causal_claim_unproven",
    ):
        validate_analysis_interpretation(causal, evidence=bundle)

    # CONTROL 2: an association-only sentence is still accepted, proving the
    # gate is causal-specific and not a blanket ban on statements.
    association_only = AnalysisInterpretation(
        summary=AnalysisStatement(
            text="The complaint count fell within the reported window."
        )
    )
    assert (
        validate_analysis_interpretation(association_only, evidence=bundle)
        is association_only
    )


def test_causal_claim_citing_facts_without_declared_design_is_rejected() -> None:
    bundle = _bundle()
    fact_id = bundle.facts[0].fact_id
    unsupported = AnalysisInterpretation(
        summary=AnalysisStatement(text=_CAUSAL_SENTENCE, fact_ids=(fact_id,))
    )
    with pytest.raises(
        AnalysisEvidenceError,
        match="analysis_interpretation_causal_evidence_insufficient",
    ):
        validate_analysis_interpretation(unsupported, evidence=bundle)


def test_causal_claim_with_declared_evidence_is_accepted() -> None:
    probe = _bundle()
    fact_id = probe.facts[0].fact_id
    supported = _bundle(
        causal_support=(
            AnalysisCausalSupport(
                fact_id=fact_id,
                design="comparative_windows",
                contrast_ref="contrast.promo-vs-baseline",
            ),
        )
    )
    assert supported.causal_support[0].fact_id == fact_id

    causal = AnalysisInterpretation(
        summary=AnalysisStatement(
            text="促销活动导致了投诉量下降。", fact_ids=(fact_id,)
        ),
        caveats=(AnalysisStatement(text="No uncited numeric claim is made."),),
    )
    assert validate_analysis_interpretation(causal, evidence=supported) is causal


def test_causal_support_must_reference_a_projected_fact() -> None:
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_causal_support_unknown_fact"
    ):
        _bundle(
            causal_support=(
                AnalysisCausalSupport(fact_id="0" * 64, design="explicit_mechanism"),
            )
        )


# --- 2. denominator / sampling range in provenance ---------------------------


def test_scope_provenance_carries_denominator_and_sampling_range() -> None:
    scope = AnalysisScopeProvenance(
        denominator_basis="metric_contract_denominator",
        denominator_ref="metric.complaint_rate.ratio",
        sampling_scope="bounded_sample",
        sample_limit=500,
        sampled_row_count=500,
        population_row_count=10_000,
    )
    plain = _bundle()
    bundle = _bundle(scope_provenance=scope)

    assert bundle.scope_provenance.denominator_basis == "metric_contract_denominator"
    assert bundle.scope_provenance.denominator_ref == "metric.complaint_rate.ratio"
    assert bundle.scope_provenance.sampling_scope == "bounded_sample"
    assert bundle.scope_provenance.sample_limit == 500
    assert bundle.scope_provenance.sampled_row_count == 500
    assert bundle.scope_provenance.population_row_count == 10_000
    # Scope is part of the bundle identity, so two analyses that differ only in
    # denominator/sampling are not the same evidence.
    assert bundle.checksum != plain.checksum

    dumped = bundle.model_dump(mode="json")
    assert dumped["scope_provenance"]["denominator_basis"] == "metric_contract_denominator"
    assert dumped["scope_provenance"]["sampling_scope"] == "bounded_sample"

    projection = project_analysis_model_input(bundle, analysis_goal="explain")
    assert '"denominator_basis":"metric_contract_denominator"' in (
        projection.messages[1].content.replace(" ", "")
    )
    assert '"sampling_scope":"bounded_sample"' in (
        projection.messages[1].content.replace(" ", "")
    )

    # The honest default claims nothing.
    assert plain.scope_provenance.denominator_basis == "not_declared"
    assert plain.scope_provenance.sampling_scope == "not_declared"
    assert "analysis_scope_undeclared" in plain.degradation_flags


def test_scope_provenance_refuses_incoherent_combinations() -> None:
    with pytest.raises(ValidationError):
        AnalysisScopeProvenance(sampling_scope="bounded_sample")
    with pytest.raises(ValidationError):
        AnalysisScopeProvenance(denominator_basis="metric_contract_denominator")
    with pytest.raises(ValidationError):
        AnalysisScopeProvenance(
            sampling_scope="bounded_sample",
            sample_limit=10,
            sampled_row_count=11,
            population_row_count=10,
        )
    with pytest.raises(ValidationError):
        AnalysisScopeProvenance(sampling_scope="full_scope_census", sample_limit=10)


# --- 3. manual origin is not an automated base table -------------------------


def _manual() -> AnalysisManualOriginEvidence:
    return AnalysisManualOriginEvidence.create(
        label="store manager reported a staffing shortage",
        value=None,
        asserted_by_role="business_operator",
        attestation_ref="manual.ops-note-20260920",
        asserted_at=datetime(2026, 9, 20, tzinfo=UTC),
    )


def test_manual_origin_and_automated_basis_are_distinguishable() -> None:
    manual = _manual()
    bundle = _bundle(manual_origin_evidence=(manual,))

    automated_fact = bundle.facts[0]
    assert automated_fact.source_kind == "approved_aggregate"
    assert automated_fact.origin == "automated_governed_source"
    assert bundle.manual_origin_evidence[0].source_kind == "manual_origin"

    provenance = bundle.authority_provenance
    assert provenance.manual_origin_ids == (manual.evidence_id,)
    assert provenance.receipt_step_ids == (automated_fact.step_id,)
    assert set(provenance.manual_origin_ids) & set(
        fact.fact_id for fact in bundle.facts
    ) == set()
    assert "analysis_manual_origin_evidence_present" in bundle.degradation_flags

    # A manual assertion can never be smuggled into the automated fact channel.
    with pytest.raises(ValidationError):
        AnalysisEvidenceFact(
            fact_id="1" * 64,
            step_id="fetch_manual",
            metric_key="manual.note",
            status="grounded",
            value=1,
            output_digest="2" * 64,
            source_kind="manual_origin",
        )

    # Citation namespaces are separate: the manual id is unknown as a fact and
    # valid only as manual evidence.
    fact_id = automated_fact.fact_id
    as_fact = AnalysisInterpretation(
        summary=AnalysisStatement(
            text="The staffing note explains the drop.",
            fact_ids=(manual.evidence_id,),
        )
    )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_interpretation_unknown_fact"
    ):
        validate_analysis_interpretation(as_fact, evidence=bundle)

    as_manual = AnalysisInterpretation(
        summary=AnalysisStatement(
            text="The staffing note explains the drop.",
            manual_evidence_ids=(manual.evidence_id,),
        ),
        observations=(
            AnalysisStatement(
                text="The governed count is 42.", fact_ids=(fact_id,)
            ),
        ),
    )
    assert validate_analysis_interpretation(as_manual, evidence=bundle) is as_manual


def test_manual_origin_identity_is_content_derived_and_deterministic() -> None:
    first = _manual()
    second = _manual()
    assert first.evidence_id == second.evidence_id
    tampered = first.model_copy(update={"label": "rewritten after the fact"})
    with pytest.raises(ValidationError):
        AnalysisManualOriginEvidence.model_validate(tampered.model_dump(mode="json"))
    changed = AnalysisManualOriginEvidence.create(
        label="store manager reported a staffing shortage",
        value=7,
        asserted_by_role="business_operator",
        attestation_ref="manual.ops-note-20260920",
        asserted_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    assert changed.evidence_id != first.evidence_id


# --- 4. cross-round drilldown budget -----------------------------------------


def test_cross_round_budget_accumulates_and_never_resets() -> None:
    policy = DrilldownBudgetPolicy(max_rounds=3, max_model_calls=5)
    ledger = DrilldownBudgetLedger(policy)

    ledger.begin_round()
    ledger.begin_model_call()
    assert ledger.usage.rounds == 1
    assert ledger.usage.model_calls == 1

    # Round 2 inherits the already-consumed quota instead of rebuilding it.
    ledger.begin_round()
    ledger.begin_model_call()
    assert ledger.usage.rounds == 2
    assert ledger.usage.model_calls == 2

    # A carried checkpoint preserves the accumulated usage across a round.
    restored = DrilldownBudgetLedger.resume(policy=policy, record=ledger.checkpoint())
    assert restored.usage.rounds == 2
    assert restored.usage.model_calls == 2
    restored.begin_round()
    assert restored.usage.rounds == 3

    # Different axis from the engine's per-request resume carry-forward.
    assert isinstance(ledger.checkpoint(), DrilldownBudgetRecord)
    assert isinstance(RouteBudgetLedger(route="standard"), RouteBudgetLedger)
    assert RouteBudgetLedger(route="standard").model_calls == 0


def test_cross_round_budget_stops_at_the_limit() -> None:
    policy = DrilldownBudgetPolicy(max_rounds=2, max_model_calls=1)
    ledger = DrilldownBudgetLedger(policy)
    ledger.begin_round()
    ledger.begin_model_call()
    ledger.begin_round()

    with pytest.raises(BudgetExceeded):
        ledger.begin_round()
    assert ledger.stop_reason == "drilldown_round_budget_exhausted"
    assert ledger.usage.rounds == 2
    assert ledger.usage.model_calls == 1

    # Once halted, nothing may consume more quota.
    with pytest.raises(BudgetExceeded):
        ledger.begin_model_call()

    model_policy = DrilldownBudgetPolicy(max_rounds=4, max_model_calls=1)
    model_ledger = DrilldownBudgetLedger(model_policy)
    model_ledger.begin_round()
    model_ledger.begin_model_call()
    with pytest.raises(BudgetExceeded):
        model_ledger.begin_model_call()
    assert model_ledger.stop_reason == "drilldown_model_budget_exhausted"

    with pytest.raises(ValueError):
        DrilldownBudgetLedger.resume(
            policy=model_policy,
            record=DrilldownBudgetLedger(
                DrilldownBudgetPolicy(max_rounds=4, max_model_calls=2)
            ).checkpoint(),
        )


# --- 5. business / risk decision producers -----------------------------------


def _context() -> ContextBundle:
    return ContextBundle(
        semantic_release_id=UUID("11111111-1111-1111-1111-111111111111"),
        schema_snapshot_id=UUID("22222222-2222-2222-2222-222222222222"),
        domains=("complaint",),
        asset_ids=("metric.complaint_rate",),
        resolution_status="resolved",
        unresolved_slots=(),
    )


def _plan() -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="complaint",
        metric_keys=("metric.complaint_rate",),
        time_range=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        grain="month",
        source_strategy="aggregate_first",
        unresolved_slots=(),
    )


def test_business_confirmation_producer_emits_a_valid_hitl_request() -> None:
    context, plan = _context(), _plan()
    request = business_confirmation_request(
        plan=plan,
        context=context,
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="a" * 64,
        issue_codes=("custom_metric_plan_confirmation",),
    )
    assert request.decision_kind == "business_confirmation"
    assert request.allowed_actions == ("confirm", "modify", "reject", "cancel")
    assert request.unresolved_slots == ()
    assert request.plan_sha256 == plan.checksum
    assert request.context_checksum == context.checksum
    # Reuses the existing validation boundary and identity.
    assert revalidate_request(request) == request
    assert request == business_confirmation_request(
        plan=plan,
        context=context,
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="a" * 64,
        issue_codes=("custom_metric_plan_confirmation",),
    )

    decision = HITLDecision(
        request_id=request.request_id,
        request_version=request.version,
        action="confirm",
        idempotency_key="idem-business-1",
    )
    assert decision.validate_against(request) == ()
    token = resume_token(request=request, decision=decision)
    assert token.request_id == request.request_id

    with pytest.raises(DecisionContractError):
        business_confirmation_request(
            plan=plan,
            context=context,
            policy_version="plan-validation.bootstrap.v1",
            policy_checksum="a" * 64,
            issue_codes=(),
        )


def test_risk_policy_producer_emits_a_valid_hitl_request_and_refuses_resolution() -> None:
    context, plan = _context(), _plan()
    request = risk_policy_decision_request(
        plan=plan,
        context=context,
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="a" * 64,
        issue_codes=("high_risk_execution",),
        safe_summary="risk acceptance required",
    )
    assert request.decision_kind == "risk_policy_decision"
    assert request.allowed_actions == ("confirm", "reject", "cancel")
    assert request.unresolved_slots == ()
    assert revalidate_request(request) == request

    reject = HITLDecision(
        request_id=request.request_id,
        request_version=request.version,
        action="reject",
        idempotency_key="idem-risk-1",
    )
    assert reject.validate_against(request) == ()
    assert resume_token(request=request, decision=reject).request_id == request.request_id

    # A risk acceptance can never carry a resolution payload.
    resolve = HITLDecision(
        request_id=request.request_id,
        request_version=request.version,
        action="resolve",
        idempotency_key="idem-risk-2",
    )
    assert "decision_action_not_allowed" in resolve.validate_against(request)

    with pytest.raises(DecisionContractError):
        risk_policy_decision_request(
            plan=plan,
            context=context,
            policy_version="plan-validation.bootstrap.v1",
            policy_checksum="not-a-checksum",
            issue_codes=("high_risk_execution",),
        )
