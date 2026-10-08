"""PlanCompiler.compile_ad_hoc: pre-resolved noncanonical AD_HOC compilation."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from src.nl2sql.agents.dynamic_calc.trusted_templates import trusted_template_registry
from src.nl2sql.contracts import (
    AdHocCalculationStep,
    ContextBundle,
    ExecutionPlan,
    FetchMetricStep,
    PlanValidationIssue,
    PlanValidationRecord,
    QueryPlan,
    RequestIdentity,
    TimeRange,
    VerifyStep,
)
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
    ApprovedCalculationInput,
)
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.planning import (
    PlanCompilationError,
    PlanCompiler,
    PlanValidationError,
    PlanValidator,
)
from src.nl2sql.semantic.calculation_contract import (
    AggregateOperand,
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    ParameterSpec,
    derived_output_id,
)

RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
REQUEST_ID = UUID("33333333-3333-3333-3333-333333333333")


def _identity() -> RequestIdentity:
    return RequestIdentity(
        request_id=REQUEST_ID,
        user_id="analyst",
        permissions=frozenset({"metrics:read"}),
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


def _query_plan(**overrides: object) -> QueryPlan:
    base: dict[str, object] = {
        "intent": "metric",
        "domain": "finance",
        "metric_keys": ("metric.revenue", "metric.stores"),
        "time_range": TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        "grain": "month",
        "source_strategy": "aggregate_first",
        "required_permissions": ("metrics:read",),
    }
    base.update(overrides)
    return QueryPlan(**base)


def _spec(**overrides: object) -> CalculationSpec:
    base: dict[str, object] = {
        "calculation_id": "adhoc.revenue_per_store",
        "expression": BinaryOperand(
            op="divide",
            left=AggregateOperand(function="sum", role="numerator"),
            right=AggregateOperand(function="count_distinct", role="denominator"),
        ),
        "inputs": (
            CalculationInputSpec(
                role="numerator", provenance="published_gold", metric_key="metric.revenue"
            ),
            CalculationInputSpec(
                role="denominator", provenance="published_gold", metric_key="metric.stores"
            ),
        ),
        "unit": "ratio",
        "precision": 4,
        "rounding": "half_up",
    }
    base.update(overrides)
    return CalculationSpec(**base)


def _binding(spec: CalculationSpec, **overrides: object) -> CalculationExecutionBinding:
    base: dict[str, object] = {
        "calculation_id": spec.calculation_id,
        "spec_checksum": spec.checksum,
        "parameters": (),
    }
    base.update(overrides)
    return CalculationExecutionBinding(**base)


def _validation(plan: QueryPlan, context: ContextBundle) -> PlanValidationRecord:
    return PlanValidator().validate_query_plan(
        plan=plan, context=context, identity=_identity()
    )


def _compile(
    *,
    plan: QueryPlan | None = None,
    context: ContextBundle | None = None,
    validation: PlanValidationRecord | None = None,
    spec: CalculationSpec | None = None,
    binding: CalculationExecutionBinding | None = None,
    compiler: PlanCompiler | None = None,
) -> ExecutionPlan:
    resolved_plan = plan if plan is not None else _query_plan()
    resolved_context = context if context is not None else _context()
    resolved_spec = spec if spec is not None else _spec()
    resolved_binding = (
        binding if binding is not None else _binding(resolved_spec)
    )
    resolved_validation = (
        validation
        if validation is not None
        else _validation(resolved_plan, resolved_context)
    )
    return (compiler or PlanCompiler()).compile_ad_hoc(
        plan=resolved_plan,
        context=resolved_context,
        validation=resolved_validation,
        calculation_spec=resolved_spec,
        execution_binding=resolved_binding,
    )


# B6, B8 ----------------------------------------------------------------------
def test_compile_ad_hoc_emits_the_deterministic_dag_and_validates_allow() -> None:
    spec = _spec()
    binding = _binding(spec)
    plan = _query_plan()
    context = _context()
    execution_plan = _compile(plan=plan, context=context, spec=spec, binding=binding)

    assert [step.kind for step in execution_plan.steps] == [
        "fetch_metric",
        "fetch_metric",
        "ad_hoc_calculation",
        "verify",
    ]
    derived = derived_output_id(spec, binding)
    fetches = [s for s in execution_plan.steps if isinstance(s, FetchMetricStep)]
    for fetch in fetches:
        assert fetch.ad_hoc_spec_checksum == spec.checksum
        assert fetch.ad_hoc_derived_output_id == derived
        assert fetch.calculation_input_role is None
        assert derived not in fetch.metric_keys
    calculation = execution_plan.steps[2]
    assert isinstance(calculation, AdHocCalculationStep)
    assert calculation.derived_output_id == derived
    assert calculation.input_refs == {
        "numerator": "fetch_numerator.value",
        "denominator": "fetch_denominator.value",
    }
    assert calculation.depends_on == ("fetch_numerator", "fetch_denominator")
    verify = execution_plan.steps[3]
    assert isinstance(verify, VerifyStep)
    assert verify.input_refs == ("calculate_adhoc",)

    record = PlanValidator().validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=RouteBudgetLedger(route="standard").limits,
    )
    assert record.outcome == "allow"
    assert record.issues == ()
    # The compiler is deterministic.
    again = _compile(plan=plan, context=context, spec=spec, binding=binding)
    assert again.checksum == execution_plan.checksum


# B2 --------------------------------------------------------------------------
def test_compile_ad_hoc_rejects_stale_or_non_allow_validation() -> None:
    plan = _query_plan()
    context = _context()
    spec = _spec()
    binding = _binding(spec)
    with pytest.raises(PlanCompilationError, match="query_plan_validation_hash_mismatch"):
        _compile(
            plan=plan,
            context=context,
            spec=spec,
            binding=binding,
            validation=_validation(_query_plan(metric_keys=("metric.revenue",)), context),
        )
    denied = PlanValidationRecord(
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="a" * 64,
        outcome="deny",
        query_plan_sha256=plan.checksum,
        context_checksum=context.checksum,
        issues=(
            PlanValidationIssue(
                code="query_plan_validation_denied",
                safe_message="denied",
            ),
        ),
    )
    with pytest.raises(PlanValidationError):
        _compile(plan=plan, context=context, spec=spec, binding=binding, validation=denied)


def test_compile_ad_hoc_rejects_unresolved_slots() -> None:
    plan = _query_plan(unresolved_slots=("time",))
    context = _context()
    spec = _spec()
    binding = _binding(spec)
    allow = PlanValidationRecord(
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="a" * 64,
        outcome="allow",
        query_plan_sha256=plan.checksum,
        context_checksum=context.checksum,
    )
    with pytest.raises(PlanCompilationError, match="ad_hoc_calculation_unresolved_slots"):
        _compile(plan=plan, context=context, spec=spec, binding=binding, validation=allow)


# B4 --------------------------------------------------------------------------
def test_compile_ad_hoc_rejects_binding_mismatches() -> None:
    spec = _spec()
    plan = _query_plan()
    context = _context()
    with pytest.raises(
        PlanCompilationError, match="ad_hoc_calculation_binding_identity_mismatch"
    ):
        _compile(
            plan=plan,
            context=context,
            spec=spec,
            binding=_binding(spec, calculation_id="adhoc.other"),
        )
    with pytest.raises(
        PlanCompilationError, match="ad_hoc_calculation_binding_spec_mismatch"
    ):
        _compile(
            plan=plan,
            context=context,
            spec=spec,
            binding=_binding(spec, spec_checksum="0" * 64),
        )


def test_compile_ad_hoc_rejects_a_contract_invalid_binding() -> None:
    spec = _spec(
        parameters=(ParameterSpec(name="month", value_type="string", required=True),)
    )
    plan = _query_plan()
    context = _context()
    with pytest.raises(PlanCompilationError, match="ad_hoc_calculation_binding_invalid"):
        _compile(plan=plan, context=context, spec=spec, binding=_binding(spec))


# B3 --------------------------------------------------------------------------
def test_compile_ad_hoc_rejects_unused_declared_inputs() -> None:
    spec = _spec(
        inputs=(
            CalculationInputSpec(
                role="numerator", provenance="published_gold", metric_key="metric.revenue"
            ),
            CalculationInputSpec(
                role="denominator", provenance="published_gold", metric_key="metric.stores"
            ),
            CalculationInputSpec(
                role="unused", provenance="published_gold", metric_key="metric.other"
            ),
        )
    )
    with pytest.raises(PlanCompilationError, match="ad_hoc_calculation_input_unused"):
        _compile(spec=spec)


def test_compile_ad_hoc_rejects_source_plan_mismatch() -> None:
    spec = _spec()
    binding = _binding(spec)
    with pytest.raises(
        PlanCompilationError, match="ad_hoc_calculation_source_plan_mismatch"
    ):
        _compile(
            plan=_query_plan(metric_keys=("metric.revenue",)),
            context=_context(),
            spec=spec,
            binding=binding,
        )


def test_compile_ad_hoc_rejects_unresolved_context_metric() -> None:
    spec = _spec()
    binding = _binding(spec)
    context = _context(asset_ids=("metric.revenue",))
    plan = _query_plan()
    allow = PlanValidationRecord(
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="a" * 64,
        outcome="allow",
        query_plan_sha256=plan.checksum,
        context_checksum=context.checksum,
    )
    with pytest.raises(PlanCompilationError, match="ad_hoc_calculation_context_missing"):
        _compile(
            plan=plan,
            context=context,
            spec=spec,
            binding=binding,
            validation=allow,
        )


# B7 --------------------------------------------------------------------------
def test_compile_ad_hoc_rejects_unsupported_role_spelling() -> None:
    spec = _spec(
        expression=BinaryOperand(
            op="divide",
            left=AggregateOperand(function="sum", role="numerator"),
            right=AggregateOperand(function="count_distinct", role="_denominator"),
        ),
        inputs=(
            CalculationInputSpec(
                role="numerator", provenance="published_gold", metric_key="metric.revenue"
            ),
            CalculationInputSpec(
                role="_denominator",
                provenance="published_gold",
                metric_key="metric.stores",
            ),
        ),
    )
    with pytest.raises(PlanCompilationError, match="ad_hoc_calculation_role_unsupported"):
        _compile(spec=spec)


# B5 --------------------------------------------------------------------------
def _catalog_binding(metric_key: str) -> ApprovedCalculationBinding:
    meta = trusted_template_registry.metadata("ratio")
    return ApprovedCalculationBinding(
        canonical_metric_key=metric_key,
        template_id="ratio",
        template_version=meta.version,
        template_checksum=meta.checksum,
        inputs=(
            ApprovedCalculationInput(
                role="numerator",
                metric_key="metric.other",
                metric_contract_sha256="b" * 64,
            ),
        ),
        binding_revision="1",
        semantic_release_id="release-1",
        semantic_release_checksum="c" * 64,
    )


def test_compile_ad_hoc_refuses_a_catalog_bound_source() -> None:
    catalog = ApprovedCalculationCatalog([_catalog_binding("metric.revenue")])
    with pytest.raises(
        PlanCompilationError, match="ad_hoc_calculation_input_catalog_bound"
    ):
        _compile(compiler=PlanCompiler(calculation_catalog=catalog))
