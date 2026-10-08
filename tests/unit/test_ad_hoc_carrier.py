"""QUERY AD_HOC noncanonical execution carrier (contracts + executor + grounding)."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.nl2sql.contracts import (
    AdHocCalculationStep,
    AnswerFact,
    ContextBundle,
    ExecutionPlan,
    ExecutionReceipt,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStep,
    PlanStepReceipt,
    QueryPlan,
    RequestIdentity,
    TimeRange,
    TrustedCalculationStep,
    VerifyStep,
)
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
)
from src.nl2sql.orchestration.grounding import build_answer_facts, render_grounded_answer
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
from src.nl2sql.semantic.calculation_contract import (
    AggregateOperand,
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    derived_output_id,
)

_EXPECTED_POLICY = PlanValidator()
RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
SQL_FINGERPRINT = "a" * 64


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="adhoc.revenue_per_store",
        expression=BinaryOperand(
            op="divide",
            left=AggregateOperand(function="sum", role="numerator"),
            right=AggregateOperand(function="count_distinct", role="denominator"),
        ),
        inputs=(
            CalculationInputSpec(
                role="numerator",
                provenance="published_gold",
                metric_key="metric.revenue",
            ),
            CalculationInputSpec(
                role="denominator",
                provenance="published_gold",
                metric_key="metric.stores",
            ),
        ),
        unit="ratio",
        precision=4,
        rounding="half_up",
    )


def _binding(spec: CalculationSpec) -> CalculationExecutionBinding:
    return CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(),
    )


def _query_plan() -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=("metric.revenue", "metric.stores"),
        time_range=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=("metrics:read",),
    )


def _context(
    *, asset_ids: tuple[str, ...] = ("metric.revenue", "metric.stores")
) -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE_ID,
        schema_snapshot_id=SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=asset_ids,
        resolution_status="resolved",
    )


def _execution_plan(
    query_plan: QueryPlan,
    context: ContextBundle,
    spec: CalculationSpec,
    binding: CalculationExecutionBinding,
) -> ExecutionPlan:
    derived = derived_output_id(spec, binding)
    return ExecutionPlan(
        query_plan_sha256=query_plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(
            FetchMetricStep(
                step_id="fetch_numerator",
                metric_keys=("metric.revenue",),
                ad_hoc_input_role="numerator",
                ad_hoc_spec_checksum=spec.checksum,
                ad_hoc_derived_output_id=derived,
            ),
            FetchMetricStep(
                step_id="fetch_denominator",
                metric_keys=("metric.stores",),
                ad_hoc_input_role="denominator",
                ad_hoc_spec_checksum=spec.checksum,
                ad_hoc_derived_output_id=derived,
            ),
            AdHocCalculationStep(
                step_id="calculate_adhoc",
                calculation_spec=spec,
                execution_binding=binding,
                input_refs={
                    "numerator": "fetch_numerator.value",
                    "denominator": "fetch_denominator.value",
                },
                depends_on=("fetch_numerator", "fetch_denominator"),
                derived_output_id=derived,
            ),
            VerifyStep(
                step_id="verify_result",
                input_refs=("calculate_adhoc",),
                invariant_ids=("typed_result_present",),
                depends_on=("calculate_adhoc",),
            ),
        ),
    )


class _MetricRunner:
    def __init__(self, values: dict[str, float]) -> None:
        self.values = values
        self.prepare_calls = 0
        self.execute_calls = 0

    async def prepare(
        self,
        *,
        step: FetchMetricStep,
        query_plan: QueryPlan,
        context: ContextBundle,
    ) -> PreparedMetricStep:
        del query_plan, context
        self.prepare_calls += 1
        fingerprint = hashlib.sha256(step.metric_keys[0].encode()).hexdigest()
        return PreparedMetricStep(
            sql_fingerprint=fingerprint,
            join_hops=0,
            payload={"metric": step.metric_keys[0], "fingerprint": fingerprint},
        )

    async def execute(
        self,
        prepared: PreparedMetricStep,
        *,
        timeout_ms: int,
    ) -> MetricStepResult:
        del timeout_ms
        self.execute_calls += 1
        metric = prepared.payload["metric"]  # type: ignore[index]
        fingerprint = prepared.payload["fingerprint"]  # type: ignore[index]
        return MetricStepResult(
            value={"value": self.values[metric]},
            receipt=ExecutionReceipt(
                datasource="synthetic",
                readonly_role="fixture_reader",
                elapsed_ms=1,
                row_count=1,
                sql_fingerprint=fingerprint,
                policy_version="query-gateway.test.v1",
                policy_outcome="allow",
            ),
        )


class _AdHocRunner:
    def __init__(self, value: object = 2.5) -> None:
        self.value = value
        self.calls = 0
        self.last_step: AdHocCalculationStep | None = None
        self.last_inputs: dict[str, object] | None = None

    async def execute(
        self,
        *,
        step: AdHocCalculationStep,
        inputs: dict[str, Any],
    ) -> Any:
        self.calls += 1
        self.last_step = step
        self.last_inputs = inputs
        return self.value


def _validate(execution_plan: ExecutionPlan, query_plan: QueryPlan, context: ContextBundle):
    return PlanValidator().validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=query_plan,
        context=context,
        route_budget=RouteBudgetLedger(route="standard").limits,
    )


# 1, 2 ------------------------------------------------------------------------
def test_ad_hoc_step_is_a_distinct_kind_without_canonical_authority() -> None:
    spec = _spec()
    binding = _binding(spec)
    plan = _execution_plan(_query_plan(), _context(), spec, binding)
    ad_hoc = plan.steps[2]
    assert isinstance(ad_hoc, AdHocCalculationStep)
    assert ad_hoc.kind == "ad_hoc_calculation"
    assert "metric_key" not in AdHocCalculationStep.model_fields
    assert "output_metric_key" not in AdHocCalculationStep.model_fields
    assert "binding_checksum" not in AdHocCalculationStep.model_fields
    for field in ("output_metric_key", "binding_checksum", "template_id", "canonical"):
        with pytest.raises(ValidationError):
            AdHocCalculationStep(
                step_id="calculate_adhoc",
                calculation_spec=spec,
                execution_binding=binding,
                input_refs={
                    "numerator": "fetch_numerator.value",
                    "denominator": "fetch_denominator.value",
                },
                depends_on=("fetch_numerator", "fetch_denominator"),
                derived_output_id=derived_output_id(spec, binding),
                **{field: "x"},
            )


# 3 ---------------------------------------------------------------------------
def test_calculation_spec_still_rejects_injected_authority_fields() -> None:
    with pytest.raises(ValidationError):
        CalculationSpec(**{**_spec().model_dump(), "canonical": True})
    with pytest.raises(ValidationError):
        CalculationSpec(**{**_spec().model_dump(), "saved": True})


# 4, 5 ------------------------------------------------------------------------
def test_execution_binding_and_input_refs_must_match_the_spec() -> None:
    spec = _spec()
    binding = _binding(spec)
    derived = derived_output_id(spec, binding)
    wrong_binding = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum="0" * 64,
        parameters=(),
    )
    with pytest.raises(ValidationError):
        AdHocCalculationStep(
            step_id="calculate_adhoc",
            calculation_spec=spec,
            execution_binding=wrong_binding,
            input_refs={
                "numerator": "fetch_numerator.value",
                "denominator": "fetch_denominator.value",
            },
            depends_on=("fetch_numerator", "fetch_denominator"),
            derived_output_id=derived,
        )
    with pytest.raises(ValidationError):
        AdHocCalculationStep(
            step_id="calculate_adhoc",
            calculation_spec=spec,
            execution_binding=binding,
            input_refs={"numerator": "fetch_numerator.value"},
            depends_on=("fetch_numerator", "fetch_denominator"),
            derived_output_id=derived,
        )
    with pytest.raises(ValidationError):
        AdHocCalculationStep(
            step_id="calculate_adhoc",
            calculation_spec=spec,
            execution_binding=binding,
            input_refs={
                "numerator": "fetch_numerator.value",
                "denominator": "fetch_denominator.value",
                "extra": "fetch_extra.value",
            },
            depends_on=("fetch_numerator", "fetch_denominator"),
            derived_output_id=derived,
        )


# 6, 7 ------------------------------------------------------------------------
def test_derived_output_id_is_deterministic_bounded_and_never_a_metric_key() -> None:
    spec = _spec()
    binding = _binding(spec)
    first = derived_output_id(spec, binding)
    second = derived_output_id(spec, binding)
    assert first == second
    assert re.fullmatch(r"adhoc_[0-9a-f]{32}", first)
    other = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum="0" * 64,
        parameters=(),
    )
    assert derived_output_id(spec, other) != first
    assert first not in _query_plan().metric_keys


# 8, 9 ------------------------------------------------------------------------
def test_ad_hoc_fetch_provenance_is_all_or_none_and_exclusive_with_canonical() -> None:
    with pytest.raises(ValidationError):
        FetchMetricStep(step_id="fetch_numerator", metric_keys=("metric.revenue",), ad_hoc_input_role="numerator")
    with pytest.raises(ValidationError):
        FetchMetricStep(
            step_id="fetch_numerator",
            metric_keys=("metric.revenue",),
            calculation_input_role="numerator",
            calculation_binding_checksum="a" * 64,
            calculation_output_metric_key="metric.out",
            ad_hoc_input_role="numerator",
            ad_hoc_spec_checksum="b" * 64,
            ad_hoc_derived_output_id="adhoc_" + "c" * 32,
        )


# 10, 11, 12 ------------------------------------------------------------------
def test_ad_hoc_closure_failures_are_denied() -> None:
    spec = _spec()
    binding = _binding(spec)
    derived = derived_output_id(spec, binding)
    query_plan = _query_plan()
    context = _context()

    orphan = ExecutionPlan(
        query_plan_sha256=query_plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(
            FetchMetricStep(
                step_id="fetch_numerator",
                metric_keys=("metric.revenue",),
                ad_hoc_input_role="numerator",
                ad_hoc_spec_checksum=spec.checksum,
                ad_hoc_derived_output_id=derived,
            ),
            VerifyStep(
                step_id="verify_result",
                input_refs=("fetch_numerator",),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch_numerator",),
            ),
        ),
    )
    assert "ad_hoc_calculation_dependency_rogue" in {
        issue.code for issue in _validate(orphan, query_plan, context).issues
    }

    bad_spec = ExecutionPlan(
        query_plan_sha256=query_plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(
            FetchMetricStep(
                step_id="fetch_numerator",
                metric_keys=("metric.revenue",),
                ad_hoc_input_role="numerator",
                ad_hoc_spec_checksum="0" * 64,
                ad_hoc_derived_output_id=derived,
            ),
            FetchMetricStep(
                step_id="fetch_denominator",
                metric_keys=("metric.stores",),
                ad_hoc_input_role="denominator",
                ad_hoc_spec_checksum=spec.checksum,
                ad_hoc_derived_output_id=derived,
            ),
            AdHocCalculationStep(
                step_id="calculate_adhoc",
                calculation_spec=spec,
                execution_binding=binding,
                input_refs={
                    "numerator": "fetch_numerator.value",
                    "denominator": "fetch_denominator.value",
                },
                depends_on=("fetch_numerator", "fetch_denominator"),
                derived_output_id=derived,
            ),
            VerifyStep(
                step_id="verify_result",
                input_refs=("calculate_adhoc",),
                invariant_ids=("typed_result_present",),
                depends_on=("calculate_adhoc",),
            ),
        ),
    )
    assert "ad_hoc_calculation_dependency_spec_mismatch" in {
        issue.code for issue in _validate(bad_spec, query_plan, context).issues
    }


# 13 --------------------------------------------------------------------------
def test_ad_hoc_input_metric_must_be_resolved_in_context() -> None:
    spec = _spec()
    binding = _binding(spec)
    plan = _execution_plan(_query_plan(), _context(), spec, binding)
    context = _context(asset_ids=("metric.stores",))
    codes = {issue.code for issue in _validate(plan, _query_plan(), context).issues}
    assert "ad_hoc_calculation_context_missing" in codes


# 14 --------------------------------------------------------------------------
def test_ad_hoc_dependency_fetches_consume_normal_sql_budget() -> None:
    spec = _spec()
    binding = _binding(spec)
    plan = _execution_plan(_query_plan(), _context(), spec, binding)
    fast = PlanValidator().validate_execution_plan(
        execution_plan=plan,
        query_plan=_query_plan(),
        context=_context(),
        route_budget=RouteBudgetLedger(route="fast").limits,
    )
    codes = {issue.code for issue in fast.issues}
    assert "execution_plan_sql_candidate_budget_exceeded" in codes
    assert "execution_plan_sql_execution_budget_exceeded" in codes


# 15, 18, 19, 20 --------------------------------------------------------------
@pytest.mark.asyncio
async def test_injected_runner_receives_scalars_and_emits_noncanonical_receipt() -> None:
    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    runner = _AdHocRunner(2.5)
    result = await PlanExecutor(
        metric_runner=_MetricRunner({"metric.revenue": 5, "metric.stores": 2}),
        ad_hoc_calculation_runner=runner,
    ).execute(
        query_plan=query_plan,
        context=context,
        execution_plan=plan,
        validation=_validate(plan, query_plan, context),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=RouteBudgetLedger(route="standard"),
        deadline_ms=4_000,
    )
    assert result.record.status == "succeeded"
    assert runner.calls == 1
    assert runner.last_inputs == {"numerator": 5, "denominator": 2}
    assert isinstance(runner.last_step, AdHocCalculationStep)
    assert result.outputs["calculate_adhoc"] == 2.5

    receipt = next(
        item for item in result.record.step_receipts if item.kind == "ad_hoc_calculation"
    )
    assert receipt.calculation_scope == "ad_hoc_noncanonical"
    assert receipt.calculation_spec_checksum == spec.checksum
    assert receipt.execution_binding_checksum == binding.checksum
    assert receipt.derived_output_id == derived_output_id(spec, binding)
    assert receipt.output_metric_key is None
    assert receipt.binding_checksum is None


# 17 --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_executor_without_ad_hoc_runner_fails_closed() -> None:
    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    result = await PlanExecutor(
        metric_runner=_MetricRunner({"metric.revenue": 5, "metric.stores": 2}),
    ).execute(
        query_plan=query_plan,
        context=context,
        execution_plan=plan,
        validation=_validate(plan, query_plan, context),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=RouteBudgetLedger(route="standard"),
        deadline_ms=4_000,
    )
    assert result.record.status == "failed"
    assert result.record.stop_reason == "ad_hoc_calculation_unavailable"
    assert "calculate_adhoc" not in result.outputs


# 21 --------------------------------------------------------------------------
def test_receipt_cannot_mix_canonical_and_ad_hoc_provenance() -> None:
    with pytest.raises(ValidationError):
        PlanStepReceipt(
            step_id="calculate_adhoc",
            kind="ad_hoc_calculation",
            status="succeeded",
            elapsed_ms=1,
            output_digest="a" * 64,
            calculation_spec_checksum="b" * 64,
            execution_binding_checksum="c" * 64,
            derived_output_id="adhoc_" + "d" * 32,
            calculation_scope="ad_hoc_noncanonical",
            output_metric_key="metric.fake",
        )


# 16, 22, 23, 27 --------------------------------------------------------------
@pytest.mark.asyncio
async def test_ad_hoc_grounding_uses_derived_identity_and_excludes_input_fetches() -> None:
    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    result = await PlanExecutor(
        metric_runner=_MetricRunner({"metric.revenue": 5, "metric.stores": 2}),
        ad_hoc_calculation_runner=_AdHocRunner(2.5),
    ).execute(
        query_plan=query_plan,
        context=context,
        execution_plan=plan,
        validation=_validate(plan, query_plan, context),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=RouteBudgetLedger(route="standard"),
        deadline_ms=4_000,
    )
    facts = build_answer_facts(
        query_plan=query_plan,
        execution_plan=plan,
        record=result.record,
        outputs=result.outputs,
    )
    assert len(facts) == 1
    fact = facts[0]
    assert fact.step_id == "calculate_adhoc"
    assert fact.metric_key is None
    assert fact.derived_output_id == derived_output_id(spec, binding)
    assert fact.calculation_scope == "ad_hoc_noncanonical"
    assert fact.value == 2.5
    rendered = render_grounded_answer(query_plan=query_plan, facts=facts)
    assert "Derived result" in rendered
    assert "adhoc_" in rendered


# 24, 25, 26 ------------------------------------------------------------------
def test_answer_fact_requires_exactly_one_result_identity() -> None:
    metric_fact = AnswerFact(
        fact_id="a" * 64, step_id="fetch_metrics", metric_key="metric.revenue", value=1
    )
    assert metric_fact.metric_key == "metric.revenue"
    assert metric_fact.derived_output_id is None

    derived_fact = AnswerFact(
        fact_id="b" * 64,
        step_id="calculate_adhoc",
        derived_output_id="adhoc_" + "c" * 32,
        calculation_scope="ad_hoc_noncanonical",
        value=2.5,
    )
    assert derived_fact.metric_key is None

    with pytest.raises(ValidationError):
        AnswerFact(fact_id="d" * 64, step_id="s")
    with pytest.raises(ValidationError):
        AnswerFact(
            fact_id="e" * 64,
            step_id="s",
            metric_key="metric.revenue",
            derived_output_id="adhoc_" + "f" * 32,
            calculation_scope="ad_hoc_noncanonical",
        )
    with pytest.raises(ValidationError):
        AnswerFact(
            fact_id="0" * 64,
            step_id="s",
            derived_output_id="adhoc_" + "1" * 32,
        )


# --- Phase A hardening --------------------------------------------------------
def _adhoc_receipt(
    spec: CalculationSpec,
    binding: CalculationExecutionBinding,
    *,
    step_id: str = "calculate_adhoc",
    **overrides: object,
) -> PlanStepReceipt:
    base: dict[str, object] = {
        "step_id": step_id,
        "kind": "ad_hoc_calculation",
        "status": "succeeded",
        "elapsed_ms": 1,
        "output_digest": "a" * 64,
        "calculation_spec_checksum": spec.checksum,
        "execution_binding_checksum": binding.checksum,
        "derived_output_id": derived_output_id(spec, binding),
        "calculation_scope": "ad_hoc_noncanonical",
    }
    base.update(overrides)
    return PlanStepReceipt(**base)


def _record(plan: ExecutionPlan, receipt: PlanStepReceipt) -> PlanExecutionRecord:
    return PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(receipt,),
        output_step_ids=(receipt.step_id,),
    )


def test_ad_hoc_grounding_rebinds_the_receipt_to_the_real_step() -> None:
    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    outputs: dict[str, object] = {"calculate_adhoc": 2.5}

    # 1. orphan receipt: no such step in the plan
    orphan = _adhoc_receipt(spec, binding, step_id="ghost")
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, orphan), outputs={"ghost": 2.5},
    ) == ()

    # 2. receipt pointing at a NON-AD_HOC step
    wrong_kind = _adhoc_receipt(spec, binding, step_id="fetch_numerator")
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, wrong_kind), outputs={"fetch_numerator": 2.5},
    ) == ()

    # 3. mismatched derived_output_id
    bad_id = _adhoc_receipt(
        spec, binding, derived_output_id="adhoc_" + "0" * 32
    )
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, bad_id), outputs=outputs,
    ) == ()

    # 4. mismatched spec checksum
    bad_spec = _adhoc_receipt(spec, binding, calculation_spec_checksum="0" * 64)
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, bad_spec), outputs=outputs,
    ) == ()

    # 5. mismatched execution-binding checksum
    bad_binding = _adhoc_receipt(
        spec, binding, execution_binding_checksum="0" * 64
    )
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, bad_binding), outputs=outputs,
    ) == ()

    # 7. missing request-local output -> no derived fact
    good = _adhoc_receipt(spec, binding)
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, good), outputs={},
    ) == ()

    # 6. the matching receipt still grounds exactly once
    facts = build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, good), outputs=outputs,
    )
    assert len(facts) == 1
    assert facts[0].derived_output_id == derived_output_id(spec, binding)


@pytest.mark.asyncio
async def test_executor_rejects_non_scalar_ad_hoc_inputs_before_the_runner() -> None:
    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    for bad in ({"nested": 1}, [1, 2]):
        runner = _AdHocRunner()
        result = await PlanExecutor(
            metric_runner=_MetricRunner(
                {"metric.revenue": bad, "metric.stores": 2}  # type: ignore[dict-item]
            ),
            ad_hoc_calculation_runner=runner,
        ).execute(
            query_plan=query_plan,
            context=context,
            execution_plan=plan,
            validation=_validate(plan, query_plan, context),
            expected_policy_version=_EXPECTED_POLICY.policy_version,
            expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
            budget=RouteBudgetLedger(route="standard"),
            deadline_ms=4_000,
        )
        assert result.record.status == "failed"
        assert result.record.stop_reason == "ad_hoc_calculation_scalar_input_required"
        assert runner.calls == 0


def _grounded(answer: object):
    return answer


def test_canonical_provenance_mismatch_is_reported_as_a_grounding_mismatch() -> None:
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    query_plan, context, plan = _trusted_plan()
    record = _record(plan, _trusted_receipt(template_version="9.9"))
    answer = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=record,
        outputs={"calculate_ratio": 2.5},
    )
    # still fail-closed: no fact
    assert answer.facts == ()
    # but the refusal is operator-visible through the existing evidence channel
    assert "grounding_execution_mismatch" in answer.artifact.degradation_flags
    # and the execution record is NOT reclassified
    assert record.status == "succeeded"


def test_matching_canonical_receipt_has_no_mismatch_flag() -> None:
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    query_plan, context, plan = _trusted_plan()
    record = _record(plan, _trusted_receipt())
    answer = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=record,
        outputs={"calculate_ratio": 2.5},
    )
    assert len(answer.facts) == 1
    assert "grounding_execution_mismatch" not in answer.artifact.degradation_flags


def test_ad_hoc_provenance_mismatch_is_reported_as_a_grounding_mismatch() -> None:
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    bad = _adhoc_receipt(spec, binding, derived_output_id="adhoc_" + "0" * 32)
    answer = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=_record(plan, bad),
        outputs={"calculate_adhoc": 2.5},
    )
    assert answer.facts == ()
    assert "grounding_execution_mismatch" in answer.artifact.degradation_flags


def test_matching_ad_hoc_receipt_has_no_mismatch_flag() -> None:
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    good = _adhoc_receipt(spec, binding)
    answer = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=_record(plan, good),
        outputs={"calculate_adhoc": 2.5},
    )
    assert len(answer.facts) == 1
    assert "grounding_execution_mismatch" not in answer.artifact.degradation_flags


def test_missing_or_wrong_kind_calculation_step_is_also_a_mismatch() -> None:
    """R4 found these two were silent: the isinstance guard fired before the
    provenance comparison.  A calculation receipt whose step is absent from the
    plan, or is a different step kind, is the same re-binding mismatch class."""
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    query_plan, context, plan = _trusted_plan()
    # (a) missing step: the receipt names a step_id that is not in the plan
    orphan = _trusted_receipt(step_id="ghost_calc")
    answer = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=_record(plan, orphan),
        outputs={"ghost_calc": 2.5},
    )
    assert answer.facts == ()
    assert "grounding_execution_mismatch" in answer.artifact.degradation_flags
    # (b) wrong step kind: a calculation receipt matched to a fetch step id
    wrong_kind = _trusted_receipt(step_id="fetch_numerator")
    answer2 = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=_record(plan, wrong_kind),
        outputs={"fetch_numerator": 2.5},
    )
    assert "grounding_execution_mismatch" in answer2.artifact.degradation_flags


def test_internal_dependency_fetch_and_nodata_are_not_mismatches() -> None:
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    # an internal dependency fetch is a normal FILTER, never a mismatch
    dep = PlanStepReceipt(
        step_id="fetch_numerator",
        kind="fetch_metric",
        status="succeeded",
        elapsed_ms=1,
        output_digest="a" * 64,
    )
    flagged = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=_record(plan, dep),
        outputs={"fetch_numerator": 5},
    )
    assert "grounding_execution_mismatch" not in flagged.artifact.degradation_flags
    # a legitimate unavailable/no-data result is NOT a mismatch either
    good = _adhoc_receipt(spec, binding)
    nodata = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=_record(plan, good),
        outputs={"calculate_adhoc": None},
    )
    assert len(nodata.facts) == 1
    assert "grounding_execution_mismatch" not in nodata.artifact.degradation_flags


def test_grounding_requires_a_successful_execution_record() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    failed = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="failed",
        step_receipts=(_trusted_receipt(),),
        output_step_ids=("calculate_ratio",),
        stop_reason="plan_step_failed",
    )
    assert build_answer_facts(
        query_plan=query_plan,
        execution_plan=plan,
        record=failed,
        outputs={"calculate_ratio": 2.5},
    ) == ()


def test_grounding_requires_the_step_in_output_step_ids() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    good = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(_trusted_receipt(),),
        output_step_ids=("calculate_ratio",),
    )
    tampered = good.model_copy(update={"output_step_ids": ()})
    assert build_answer_facts(
        query_plan=query_plan,
        execution_plan=plan,
        record=tampered,
        outputs={"calculate_ratio": 2.5},
    ) == ()


@pytest.mark.parametrize("field", ["template_id", "template_version"])
def test_ad_hoc_receipt_rejects_template_provenance(field: str) -> None:
    spec = _spec()
    binding = _binding(spec)
    with pytest.raises(ValidationError):
        _adhoc_receipt(spec, binding, **{field: "ratio"})


def test_canonical_metric_grounding_is_unchanged() -> None:
    query_plan = _query_plan()
    context = _context()
    plan = ExecutionPlan(
        query_plan_sha256=query_plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(
            FetchMetricStep(step_id="fetch_metrics", metric_keys=("metric.revenue",)),
            VerifyStep(
                step_id="verify_result",
                input_refs=("fetch_metrics",),
                invariant_ids=("typed_result_present",),
                depends_on=("fetch_metrics",),
            ),
        ),
    )
    receipt = PlanStepReceipt(
        step_id="fetch_metrics",
        kind="fetch_metric",
        status="succeeded",
        elapsed_ms=1,
        output_digest="a" * 64,
    )
    facts = build_answer_facts(
        query_plan=query_plan,
        execution_plan=plan,
        record=_record(plan, receipt),
        outputs={"fetch_metrics": {"value": 7}},
    )
    assert len(facts) == 1
    assert facts[0].metric_key == "metric.revenue"
    assert facts[0].derived_output_id is None


# --- Iteration 3: exact V1 DAG closure ---------------------------------------
def _valid_steps(
    spec: CalculationSpec, binding: CalculationExecutionBinding
) -> tuple[PlanStep, ...]:
    derived = derived_output_id(spec, binding)
    return (
        FetchMetricStep(
            step_id="fetch_numerator",
            metric_keys=("metric.revenue",),
            ad_hoc_input_role="numerator",
            ad_hoc_spec_checksum=spec.checksum,
            ad_hoc_derived_output_id=derived,
        ),
        FetchMetricStep(
            step_id="fetch_denominator",
            metric_keys=("metric.stores",),
            ad_hoc_input_role="denominator",
            ad_hoc_spec_checksum=spec.checksum,
            ad_hoc_derived_output_id=derived,
        ),
        AdHocCalculationStep(
            step_id="calculate_adhoc",
            calculation_spec=spec,
            execution_binding=binding,
            input_refs={
                "numerator": "fetch_numerator.value",
                "denominator": "fetch_denominator.value",
            },
            depends_on=("fetch_numerator", "fetch_denominator"),
            derived_output_id=derived,
        ),
        VerifyStep(
            step_id="verify_result",
            input_refs=("calculate_adhoc",),
            invariant_ids=("typed_result_present",),
            depends_on=("calculate_adhoc",),
        ),
    )


def _plan_with(steps: tuple[object, ...]) -> ExecutionPlan:
    query_plan = _query_plan()
    context = _context()
    return ExecutionPlan(
        query_plan_sha256=query_plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=steps,  # type: ignore[arg-type]
    )


def _codes(plan: ExecutionPlan) -> set[str]:
    return {issue.code for issue in _validate(plan, _query_plan(), _context()).issues}


def _adhoc_calc(
    spec: CalculationSpec,
    binding: CalculationExecutionBinding,
    **overrides: object,
) -> AdHocCalculationStep:
    derived = derived_output_id(spec, binding)
    base: dict[str, object] = {
        "step_id": "calculate_adhoc",
        "calculation_spec": spec,
        "execution_binding": binding,
        "input_refs": {
            "numerator": "fetch_numerator.value",
            "denominator": "fetch_denominator.value",
        },
        "depends_on": ("fetch_numerator", "fetch_denominator"),
        "derived_output_id": derived,
    }
    base.update(overrides)
    return AdHocCalculationStep(**base)


def test_valid_ad_hoc_dag_is_allowed() -> None:
    spec = _spec()
    binding = _binding(spec)
    assert _codes(_plan_with(_valid_steps(spec, binding))) == set()


def test_closure_rejects_an_extra_ordinary_fetch() -> None:
    spec = _spec()
    binding = _binding(spec)
    steps = _valid_steps(spec, binding) + (
        FetchMetricStep(step_id="fetch_extra", metric_keys=("metric.revenue",)),
    )
    codes = _codes(_plan_with(steps))
    assert "ad_hoc_calculation_dependency_provenance_missing" in codes
    assert "ad_hoc_calculation_dependency_count_mismatch" in codes
    # the extra fetch is present but is not a declared calculation dependency
    assert "ad_hoc_calculation_dependency_set_mismatch" in codes


def test_closure_rejects_an_extra_trusted_calculation() -> None:
    spec = _spec()
    binding = _binding(spec)
    steps = _valid_steps(spec, binding) + (
        TrustedCalculationStep(
            step_id="calc_trusted",
            template_id="ratio",
            input_refs={"numerator": "fetch_numerator.value"},
            depends_on=("fetch_numerator",),
        ),
    )
    assert "ad_hoc_calculation_trusted_step_forbidden" in _codes(_plan_with(steps))


def test_closure_rejects_an_unrelated_calculation_dependency() -> None:
    spec = _spec()
    binding = _binding(spec)
    steps = (
        FetchMetricStep(
            step_id="fetch_numerator",
            metric_keys=("metric.revenue",),
            ad_hoc_input_role="numerator",
            ad_hoc_spec_checksum=spec.checksum,
            ad_hoc_derived_output_id=derived_output_id(spec, binding),
        ),
        FetchMetricStep(
            step_id="fetch_denominator",
            metric_keys=("metric.stores",),
            ad_hoc_input_role="denominator",
            ad_hoc_spec_checksum=spec.checksum,
            ad_hoc_derived_output_id=derived_output_id(spec, binding),
        ),
        FetchMetricStep(step_id="fetch_extra", metric_keys=("metric.revenue",)),
        _adhoc_calc(
            spec,
            binding,
            depends_on=("fetch_numerator", "fetch_denominator", "fetch_extra"),
        ),
        VerifyStep(
            step_id="verify_result",
            input_refs=("calculate_adhoc",),
            invariant_ids=("typed_result_present",),
            depends_on=("calculate_adhoc",),
        ),
    )
    codes = _codes(_plan_with(steps))
    assert "ad_hoc_calculation_dependency_provenance_missing" in codes
    assert "ad_hoc_calculation_dependency_count_mismatch" in codes


def test_closure_rejects_a_missing_ad_hoc_dependency() -> None:
    spec = _spec()
    binding = _binding(spec)
    steps = (
        FetchMetricStep(
            step_id="fetch_numerator",
            metric_keys=("metric.revenue",),
            ad_hoc_input_role="numerator",
            ad_hoc_spec_checksum=spec.checksum,
            ad_hoc_derived_output_id=derived_output_id(spec, binding),
        ),
        # denominator fetch carries NO AD_HOC provenance
        FetchMetricStep(step_id="fetch_denominator", metric_keys=("metric.stores",)),
        _adhoc_calc(spec, binding),
        VerifyStep(
            step_id="verify_result",
            input_refs=("calculate_adhoc",),
            invariant_ids=("typed_result_present",),
            depends_on=("calculate_adhoc",),
        ),
    )
    codes = _codes(_plan_with(steps))
    assert "ad_hoc_calculation_dependency_provenance_missing" in codes
    assert "ad_hoc_calculation_dependency_count_mismatch" in codes


def test_closure_rejects_an_extra_ad_hoc_dependency_without_an_input_ref() -> None:
    spec = _spec()
    binding = _binding(spec)
    steps = _valid_steps(spec, binding) + (
        FetchMetricStep(
            step_id="fetch_extra",
            metric_keys=("metric.revenue",),
            ad_hoc_input_role="extra",
            ad_hoc_spec_checksum=spec.checksum,
            ad_hoc_derived_output_id=derived_output_id(spec, binding),
        ),
    )
    codes = _codes(_plan_with(steps))
    assert "ad_hoc_calculation_dependency_count_mismatch" in codes
    assert "ad_hoc_calculation_dependency_rogue" in codes


def test_closure_requires_exactly_one_verify() -> None:
    spec = _spec()
    binding = _binding(spec)
    without = tuple(s for s in _valid_steps(spec, binding) if s.kind != "verify")
    assert "ad_hoc_calculation_verify_missing" in _codes(_plan_with(without))
    extra_verify = _valid_steps(spec, binding) + (
        VerifyStep(
            step_id="verify_result_2",
            input_refs=("calculate_adhoc",),
            invariant_ids=("typed_result_present",),
            depends_on=("calculate_adhoc",),
        ),
    )
    assert "ad_hoc_calculation_verify_duplicate" in _codes(_plan_with(extra_verify))


def test_closure_rejects_a_misbound_verify() -> None:
    spec = _spec()
    binding = _binding(spec)
    prefix = _valid_steps(spec, binding)[:3]
    to_fetch = prefix + (
        VerifyStep(
            step_id="verify_result",
            input_refs=("fetch_numerator",),
            invariant_ids=("typed_result_present",),
            depends_on=("fetch_numerator",),
        ),
    )
    assert "ad_hoc_calculation_verify_mismatch" in _codes(_plan_with(to_fetch))
    extra_dep = prefix + (
        VerifyStep(
            step_id="verify_result",
            input_refs=("calculate_adhoc",),
            invariant_ids=("typed_result_present",),
            depends_on=("calculate_adhoc", "fetch_numerator"),
        ),
    )
    assert "ad_hoc_calculation_verify_mismatch" in _codes(_plan_with(extra_dep))
    wrong_invariant = prefix + (
        VerifyStep(
            step_id="verify_result",
            input_refs=("calculate_adhoc",),
            invariant_ids=("other_invariant",),
            depends_on=("calculate_adhoc",),
        ),
    )
    assert "ad_hoc_calculation_verify_mismatch" in _codes(_plan_with(wrong_invariant))


def test_grounding_presence_is_key_based_not_none_based() -> None:
    spec = _spec()
    binding = _binding(spec)
    plan = _execution_plan(_query_plan(), _context(), spec, binding)
    good = _adhoc_receipt(spec, binding)
    # present key with JSON null is PRESENT for the trust check
    present_null = build_answer_facts(
        query_plan=_query_plan(),
        execution_plan=plan,
        record=_record(plan, good),
        outputs={"calculate_adhoc": None},
    )
    assert len(present_null) == 1
    assert present_null[0].status == "unavailable"
    # missing key produces no derived fact at all
    missing = build_answer_facts(
        query_plan=_query_plan(),
        execution_plan=plan,
        record=_record(plan, good),
        outputs={},
    )
    assert missing == ()


def test_validator_binds_ad_hoc_inputs_to_the_query_plan_source_set() -> None:
    spec = _spec()
    binding = _binding(spec)
    context = _context()
    # (A) QueryPlan requests fewer source metrics than the spec consumes.
    narrowed = _query_plan().model_copy(update={"metric_keys": ("metric.revenue",)})
    plan = _execution_plan(narrowed, context, spec, binding)
    codes = {issue.code for issue in _validate(plan, narrowed, context).issues}
    assert "ad_hoc_calculation_source_plan_mismatch" in codes
    # (A2) fully unrequested source metrics.
    unrequested = CalculationSpec(
        calculation_id="adhoc.unrequested",
        expression=BinaryOperand(
            op="divide",
            left=AggregateOperand(function="sum", role="numerator"),
            right=AggregateOperand(function="count_distinct", role="denominator"),
        ),
        inputs=(
            CalculationInputSpec(
                role="numerator", provenance="published_gold", metric_key="metric.profit"
            ),
            CalculationInputSpec(
                role="denominator", provenance="published_gold", metric_key="metric.other"
            ),
        ),
        unit="ratio",
        precision=4,
        rounding="half_up",
    )
    plan2 = _execution_plan(narrowed, context, unrequested, _binding(unrequested))
    codes2 = {issue.code for issue in _validate(plan2, narrowed, context).issues}
    assert "ad_hoc_calculation_source_plan_mismatch" in codes2


def test_validator_rejects_nested_ad_hoc_provenance_inputs() -> None:
    spec = CalculationSpec(
        calculation_id="adhoc.nested",
        expression=BinaryOperand(
            op="divide",
            left=AggregateOperand(function="sum", role="numerator"),
            right=AggregateOperand(function="count_distinct", role="denominator"),
        ),
        inputs=(
            CalculationInputSpec(
                role="numerator", provenance="ad_hoc_metric", metric_key="metric.revenue"
            ),
            CalculationInputSpec(
                role="denominator", provenance="published_gold", metric_key="metric.stores"
            ),
        ),
        unit="ratio",
        precision=4,
        rounding="half_up",
    )
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, _binding(spec))
    codes = {issue.code for issue in _validate(plan, query_plan, context).issues}
    assert "ad_hoc_calculation_nested_input_unsupported" in codes


def _trusted_plan() -> tuple[QueryPlan, ContextBundle, ExecutionPlan]:
    query_plan = _query_plan()
    context = _context()
    plan = ExecutionPlan(
        query_plan_sha256=query_plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(
            FetchMetricStep(step_id="fetch_numerator", metric_keys=("metric.revenue",)),
            FetchMetricStep(step_id="fetch_denominator", metric_keys=("metric.stores",)),
            TrustedCalculationStep(
                step_id="calculate_ratio",
                template_id="ratio",
                input_refs={
                    "numerator": "fetch_numerator.value",
                    "denominator": "fetch_denominator.value",
                },
                depends_on=("fetch_numerator", "fetch_denominator"),
                template_version="1.0",
                template_checksum="b" * 64,
                binding_checksum="c" * 64,
                output_metric_key="metric.revenue",
            ),
            VerifyStep(
                step_id="verify_result",
                input_refs=("calculate_ratio",),
                invariant_ids=("typed_result_present",),
                depends_on=("calculate_ratio",),
            ),
        ),
    )
    return query_plan, context, plan


def _trusted_receipt(**overrides: object) -> PlanStepReceipt:
    base: dict[str, object] = {
        "step_id": "calculate_ratio",
        "kind": "trusted_calculation",
        "status": "succeeded",
        "elapsed_ms": 1,
        "output_digest": "a" * 64,
        "template_id": "ratio",
        "template_version": "1.0",
        "binding_checksum": "c" * 64,
        "output_metric_key": "metric.revenue",
    }
    base.update(overrides)
    return PlanStepReceipt(**base)


def test_grounding_refuses_a_record_from_another_execution_plan() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    receipt = _trusted_receipt()
    record = PlanExecutionRecord(
        execution_plan_checksum="0" * 64,
        status="succeeded",
        step_receipts=(receipt,),
        output_step_ids=("calculate_ratio",),
    )
    assert build_answer_facts(
        query_plan=query_plan,
        execution_plan=plan,
        record=record,
        outputs={"calculate_ratio": 2.5},
    ) == ()


def test_canonical_grounding_requires_receipt_step_agreement() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    outputs = {"calculate_ratio": 2.5}
    # matching receipt grounds exactly one canonical fact
    good = build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, _trusted_receipt()), outputs=outputs,
    )
    assert len(good) == 1 and good[0].metric_key == "metric.revenue"
    for field, bad in (
        ("template_id", "other_template"),
        ("template_version", "9.9"),
        ("binding_checksum", "d" * 64),
        ("output_metric_key", "metric.other"),
    ):
        assert build_answer_facts(
            query_plan=query_plan, execution_plan=plan,
            record=_record(plan, _trusted_receipt(**{field: bad})), outputs=outputs,
        ) == (), field


def test_orphan_fetch_receipt_grounds_nothing() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    orphan = PlanStepReceipt(
        step_id="ghost_fetch", kind="fetch_metric", status="succeeded",
        elapsed_ms=1, output_digest="a" * 64,
    )
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum, status="succeeded",
        step_receipts=(orphan,), output_step_ids=("ghost_fetch",),
    )
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan, record=record,
        outputs={"ghost_fetch": {"value": 999}},
    ) == ()
    # a fetch receipt naming a real non-fetch step must not fall back either
    wrong_kind = PlanStepReceipt(
        step_id="calculate_ratio", kind="fetch_metric", status="succeeded",
        elapsed_ms=1, output_digest="a" * 64,
    )
    record2 = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum, status="succeeded",
        step_receipts=(wrong_kind,), output_step_ids=("calculate_ratio",),
    )
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan, record=record2,
        outputs={"calculate_ratio": {"value": 999}},
    ) == ()


@pytest.mark.asyncio
async def test_compiled_ad_hoc_plan_allows_and_grounds_exactly_once() -> None:
    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    validation = PlanValidator().validate_query_plan(
        plan=query_plan,
        context=context,
        identity=RequestIdentity(
            request_id=UUID("44444444-4444-4444-4444-444444444444"),
            user_id="analyst",
            permissions=frozenset({"metrics:read"}),
        ),
    )
    plan = PlanCompiler().compile_ad_hoc(
        plan=query_plan,
        context=context,
        validation=validation,
        calculation_spec=spec,
        execution_binding=binding,
    )
    record = PlanValidator().validate_execution_plan(
        execution_plan=plan,
        query_plan=query_plan,
        context=context,
        route_budget=RouteBudgetLedger(route="standard").limits,
    )
    assert record.outcome == "allow"
    assert record.issues == ()
    result = await PlanExecutor(
        metric_runner=_MetricRunner({"metric.revenue": 5, "metric.stores": 2}),
        ad_hoc_calculation_runner=_AdHocRunner(2.5),
    ).execute(
        query_plan=query_plan,
        context=context,
        execution_plan=plan,
        validation=_validate(plan, query_plan, context),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=RouteBudgetLedger(route="standard"),
        deadline_ms=4_000,
    )
    assert result.record.status == "succeeded"
    facts = build_answer_facts(
        query_plan=query_plan,
        execution_plan=plan,
        record=result.record,
        outputs=result.outputs,
    )
    assert len(facts) == 1
    assert facts[0].derived_output_id == derived_output_id(spec, binding)
