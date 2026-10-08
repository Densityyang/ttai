"""P4-S2 canonical approved-compute kernel contracts."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.nl2sql.agents.dynamic_calc.trusted_templates import (
    TrustedTemplateError,
    trusted_template_registry,
)
from src.nl2sql.contracts import (
    ContextBundle,
    ExecutionPlan,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStepReceipt,
    PlanValidationRecord,
    QueryPlan,
    RouteBudget,
    TimeRange,
    TrustedCalculationStep,
    VerifyStep,
)
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
    ApprovedCalculationInput,
    ApprovedComputeError,
)
from src.nl2sql.orchestration.execution import PlanStepError
from src.nl2sql.orchestration.grounding import build_answer_facts
from src.nl2sql.orchestration.metric_query import (
    CompiledMetricQuery,
    GatewayMetricStepRunner,
    project_dependency_scalar,
)
from src.nl2sql.orchestration.planning import (
    PlanCompilationError,
    PlanCompiler,
    PlanValidator,
)

RELEASE = UUID("11111111-1111-1111-1111-111111111111")
SNAP = UUID("22222222-2222-2222-2222-222222222222")
RCS = "a" * 64
MCS = "b" * 64


def _binding(**o: object) -> ApprovedCalculationBinding:
    meta = trusted_template_registry.metadata("ratio")
    base: dict[str, object] = {
        "canonical_metric_key": "metric.revenue_ratio",
        "template_id": "ratio",
        "template_version": meta.version,
        "template_checksum": meta.checksum,
        "inputs": (
            ApprovedCalculationInput(role="numerator", metric_key="metric.revenue", metric_contract_sha256=MCS),
            ApprovedCalculationInput(role="denominator", metric_key="metric.stores", metric_contract_sha256=MCS),
        ),
        "binding_revision": "rev-1",
        "semantic_release_id": str(RELEASE),
        "semantic_release_checksum": RCS,
    }
    base.update(o)
    return ApprovedCalculationBinding(**base)


def _context() -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE,
        schema_snapshot_id=SNAP,
        domains=("finance",),
        asset_ids=("asset-1",),
        resolution_status="resolved",
    )


def _dependency_context() -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE,
        schema_snapshot_id=SNAP,
        domains=("finance",),
        asset_ids=("metric.revenue", "metric.stores", "asset-1"),
        resolution_status="resolved",
    )


def _plan(metric_keys: tuple[str, ...] = ("metric.revenue_ratio",)) -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=metric_keys,
        time_range=TimeRange(start=date(2026, 1, 1), end=date(2026, 1, 31)),
        grain="month",
        source_strategy="aggregate_first",
    )


def _allow(plan: QueryPlan, context: ContextBundle) -> PlanValidationRecord:
    return PlanValidationRecord(
        policy_version="p1",
        policy_checksum="c" * 64,
        outcome="allow",
        query_plan_sha256=plan.checksum,
        context_checksum=context.checksum,
    )


def _calc_step(**o: object) -> TrustedCalculationStep:
    binding = _binding()
    base: dict[str, object] = {
        "step_id": "calculate_metric",
        "template_id": "ratio",
        "template_version": binding.template_version,
        "template_checksum": binding.template_checksum,
        "binding_checksum": binding.checksum,
        "output_metric_key": "metric.revenue_ratio",
        "input_refs": {"numerator": "fetch_numerator.value", "denominator": "fetch_denominator.value"},
        "depends_on": ("fetch_numerator", "fetch_denominator"),
    }
    base.update(o)
    return TrustedCalculationStep(**base)


def _canonical_catalog() -> ApprovedCalculationCatalog:
    return ApprovedCalculationCatalog([_binding()])


def _rogue_binding() -> ApprovedCalculationBinding:
    meta = trusted_template_registry.metadata("ratio")
    return ApprovedCalculationBinding(
        canonical_metric_key="metric.rogue_ratio",
        template_id="rogue_template",
        template_version=meta.version,
        template_checksum=meta.checksum,
        inputs=(
            ApprovedCalculationInput(role="numerator", metric_key="metric.revenue", metric_contract_sha256=MCS),
            ApprovedCalculationInput(role="denominator", metric_key="metric.stores", metric_contract_sha256=MCS),
        ),
        binding_revision="rev-rogue",
        semantic_release_id=str(RELEASE),
        semantic_release_checksum=RCS,
    )


def _build(
    *,
    calc: dict[str, object] | None = None,
    fetches: tuple[tuple[str, str, str], ...] = (
        ("fetch_numerator", "numerator", "metric.revenue"),
        ("fetch_denominator", "denominator", "metric.stores"),
    ),
    fetch_overrides: dict[str, dict[str, object]] | None = None,
    extra: tuple[FetchMetricStep, ...] = (),
    refs: dict[str, str] | None = None,
    depends_on: tuple[str, ...] | None = None,
    binding: ApprovedCalculationBinding | None = None,
    suffix: str = "",
) -> tuple[ExecutionPlan, TrustedCalculationStep, ContextBundle]:
    """Build an execution plan whose calculation provenance is explicit."""

    chosen = binding or _binding()
    plan = _plan()
    context = _dependency_context()
    overrides = fetch_overrides or {}
    fetch_steps = tuple(
        FetchMetricStep(
            step_id=f"{step_id}{suffix}",
            metric_keys=(metric_key,),
            calculation_input_role=role,
            calculation_binding_checksum=chosen.checksum,
            calculation_output_metric_key=chosen.canonical_metric_key,
        ).model_copy(update=overrides.get(step_id, {}))
        for step_id, role, metric_key in fetches
    )
    calc_base: dict[str, object] = {
        "step_id": f"calculate_metric{suffix}",
        "template_id": chosen.template_id,
        "template_version": chosen.template_version,
        "template_checksum": chosen.template_checksum,
        "binding_checksum": chosen.checksum,
        "output_metric_key": chosen.canonical_metric_key,
        "input_refs": {
            item.role: f"fetch_{item.role}{suffix}.value" for item in chosen.inputs
        },
        "depends_on": tuple(step.step_id for step in fetch_steps),
    }
    if refs is not None:
        calc_base["input_refs"] = refs
    if depends_on is not None:
        calc_base["depends_on"] = depends_on
    calc_base.update(calc or {})
    calculation = TrustedCalculationStep(**calc_base)
    verify = VerifyStep(
        step_id=f"verify_result{suffix}",
        input_refs=(calculation.step_id,),
        invariant_ids=("typed_result_present",),
        depends_on=(calculation.step_id,),
    )
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(*fetch_steps, *extra, calculation, verify),
    )
    return execution_plan, calculation, context


def _with_steps(
    execution_plan: ExecutionPlan,
    *steps: FetchMetricStep | VerifyStep | TrustedCalculationStep,
) -> ExecutionPlan:
    return ExecutionPlan(
        query_plan_sha256=execution_plan.query_plan_sha256,
        semantic_release_id=execution_plan.semantic_release_id,
        schema_snapshot_id=execution_plan.schema_snapshot_id,
        policy_version=execution_plan.policy_version,
        steps=(*execution_plan.steps, *steps),
    )


def _validate_plan(
    execution_plan: ExecutionPlan,
    plan: QueryPlan,
    context: ContextBundle,
    *,
    catalog: ApprovedCalculationCatalog | None = None,
) -> PlanValidationRecord:
    return PlanValidator(
        calculation_catalog=catalog or _canonical_catalog()
    ).validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=_route_budget(),
    )


def _validate(
    execution_plan: ExecutionPlan,
    calculation: TrustedCalculationStep,
    context: ContextBundle,
    *,
    catalog: ApprovedCalculationCatalog | None = None,
) -> tuple[str, ...]:
    return (catalog or _canonical_catalog()).validate_calculation(
        step=calculation, execution_plan=execution_plan, context=context
    )


def test_registry_is_versioned_and_checksum_deterministic() -> None:
    meta = trusted_template_registry.metadata("ratio")
    assert meta.version == "1.0"
    assert meta.input_roles == ("numerator", "denominator")
    assert trusted_template_registry.registry_checksum == trusted_template_registry.registry_checksum
    with pytest.raises(TrustedTemplateError):
        trusted_template_registry.metadata("rogue_template")


def test_binding_checksum_is_stable_and_duplicate_keys_rejected() -> None:
    assert _binding().checksum == _binding().checksum
    # Frozen serialization lock, re-pinned for approved-calculation schema 1.1:
    # the inert global null/zero policy fields were removed and schema_version
    # moved to "1.1", so the accepted payload intentionally changes.
    assert (
        _binding().checksum
        == "b0b0b1485ce9b4e10730ad41d4c7c5524082b3b50b08d413e496b23f6bf97df6"
    )
    with pytest.raises(ApprovedComputeError):
        ApprovedCalculationCatalog([_binding(), _binding()])


def test_binding_rounding_requires_an_explicit_precision() -> None:
    with pytest.raises(ValidationError, match="rounding requires an explicit precision"):
        _binding(rounding="half_up")
    # precision-only and both-set remain legal and preserve their identity.
    assert _binding(precision=2).rounding is None
    both = _binding(precision=2, rounding="half_up")
    assert both.checksum == _binding(precision=2, rounding="half_up").checksum
    assert both.checksum != _binding(precision=2).checksum


def test_catalog_checksum_is_order_independent_and_content_sensitive() -> None:
    first = _binding()
    second = _rogue_binding()
    assert (
        ApprovedCalculationCatalog([first, second]).checksum
        == ApprovedCalculationCatalog([second, first]).checksum
    )
    assert (
        ApprovedCalculationCatalog([first]).checksum
        == ApprovedCalculationCatalog([first]).checksum
    )
    changed = first.model_copy(update={"binding_revision": "rev-2"})
    assert (
        ApprovedCalculationCatalog([changed]).checksum
        != ApprovedCalculationCatalog([first]).checksum
    )
    assert (
        ApprovedCalculationCatalog([]).checksum
        != ApprovedCalculationCatalog([first]).checksum
    )
    assert len(ApprovedCalculationCatalog([first]).checksum) == 64


def test_validator_policy_identity_binds_the_calculation_catalog() -> None:
    catalog = _canonical_catalog()
    assert (
        PlanValidator(calculation_catalog=catalog).policy_checksum
        == PlanValidator(calculation_catalog=_canonical_catalog()).policy_checksum
    )
    assert (
        PlanValidator(calculation_catalog=catalog).policy_checksum
        != PlanValidator().policy_checksum
    )
    other = ApprovedCalculationCatalog([_rogue_binding()])
    assert (
        PlanValidator(calculation_catalog=catalog).policy_checksum
        != PlanValidator(calculation_catalog=other).policy_checksum
    )
    assert len(PlanValidator(calculation_catalog=catalog).policy_checksum) == 64
    # No catalog configured: the legacy payload is byte-for-byte unchanged, so
    # no empty-catalog authority marker is invented.
    assert (
        PlanValidator().policy_checksum
        == "c1659573c04ee1a5da9f78e5fa16157652b0a62b648b689736db647869304cbc"
    )


def test_calculation_provenance_is_all_or_none() -> None:
    # All present is the canonical form; all absent is the legacy/unbound form.
    assert _calc_step().binding_checksum is not None
    legacy = TrustedCalculationStep(
        step_id="calculate_metric",
        template_id="ratio",
        input_refs={"numerator": "fetch_metrics.numerator", "denominator": "fetch_metrics.denominator"},
        depends_on=("fetch_metrics",),
    )
    assert legacy.binding_checksum is None
    assert legacy.output_metric_key is None
    assert legacy.template_version is None
    assert legacy.template_checksum is None
    for partial in (
        {"binding_checksum": None},
        {"output_metric_key": None},
        {"template_version": None},
        {"template_checksum": None},
        {"template_version": None, "template_checksum": None, "binding_checksum": None},
    ):
        with pytest.raises(ValueError):
            _calc_step(**partial)


def test_valid_role_fetch_ref_mapping_is_proven() -> None:
    execution_plan, calculation, context = _build()
    assert _validate(execution_plan, calculation, context) == ()


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"template_id": "mean_values"}, "trusted_calculation_template_mismatch"),
        ({"template_version": "9.9"}, "trusted_calculation_version_mismatch"),
        ({"template_checksum": "d" * 64}, "trusted_calculation_version_mismatch"),
        ({"binding_checksum": "d" * 64}, "trusted_calculation_binding_mismatch"),
        ({"output_metric_key": "metric.other"}, "trusted_calculation_binding_missing"),
        (
            {"input_refs": {"numerator": "fetch_numerator.value"}},
            "trusted_calculation_input_role_mismatch",
        ),
    ],
)
def test_calculation_identity_failures_are_fail_closed(
    overrides: dict[str, object], code: str
) -> None:
    execution_plan, calculation, context = _build(calc=overrides)
    assert code in _validate(execution_plan, calculation, context)


def test_unregistered_binding_template_fails_closed() -> None:
    rogue = _rogue_binding()
    execution_plan, calculation, context = _build(binding=rogue)
    failures = _validate(
        execution_plan,
        calculation,
        context,
        catalog=ApprovedCalculationCatalog([rogue]),
    )
    assert "trusted_calculation_template_unregistered" in failures


def test_release_mismatch_fails() -> None:
    other = _binding(semantic_release_id=str(SNAP))
    execution_plan, calculation, context = _build(binding=other)
    failures = _validate(
        execution_plan,
        calculation,
        context,
        catalog=ApprovedCalculationCatalog([other]),
    )
    assert "trusted_calculation_release_mismatch" in failures


def test_missing_dependency_role_is_denied() -> None:
    execution_plan, calculation, context = _build(
        fetches=(("fetch_denominator", "denominator", "metric.stores"),),
        refs={"numerator": "fetch_denominator.value", "denominator": "fetch_denominator.value"},
    )
    assert "trusted_calculation_dependency_missing" in _validate(execution_plan, calculation, context)


def test_duplicate_dependency_role_is_denied() -> None:
    execution_plan, calculation, context = _build(
        fetches=(
            ("fetch_numerator", "numerator", "metric.revenue"),
            ("fetch_denominator", "denominator", "metric.stores"),
            ("fetch_numerator_two", "numerator", "metric.revenue"),
        )
    )
    assert "trusted_calculation_dependency_duplicate" in _validate(execution_plan, calculation, context)


def test_role_must_fetch_its_own_metric() -> None:
    execution_plan, calculation, context = _build(
        fetches=(
            ("fetch_numerator", "numerator", "metric.stores"),
            ("fetch_denominator", "denominator", "metric.revenue"),
        )
    )
    assert "trusted_calculation_dependency_metric_mismatch" in _validate(
        execution_plan, calculation, context
    )


def test_dependency_must_carry_the_binding_checksum() -> None:
    execution_plan, calculation, context = _build(
        fetch_overrides={"fetch_numerator": {"calculation_binding_checksum": "d" * 64}}
    )
    assert "trusted_calculation_dependency_binding_mismatch" in _validate(
        execution_plan, calculation, context
    )


def test_dependency_must_claim_the_canonical_output() -> None:
    execution_plan, calculation, context = _build(
        fetch_overrides={"fetch_numerator": {"calculation_output_metric_key": "metric.other"}}
    )
    assert "trusted_calculation_dependency_output_mismatch" in _validate(
        execution_plan, calculation, context
    )


def test_input_ref_must_point_at_its_own_dependency() -> None:
    execution_plan, calculation, context = _build(
        refs={"numerator": "fetch_denominator.value", "denominator": "fetch_denominator.value"}
    )
    assert "trusted_calculation_input_ref_mismatch" in _validate(
        execution_plan, calculation, context
    )


def test_dependency_metric_must_be_resolved_in_context() -> None:
    execution_plan, calculation, _ = _build()
    assert "trusted_calculation_dependency_context_missing" in _validate(
        execution_plan, calculation, _context()
    )


def test_declared_dependency_without_provenance_is_denied() -> None:
    execution_plan, calculation, context = _build(
        fetch_overrides={
            "fetch_numerator": {
                "calculation_input_role": None,
                "calculation_binding_checksum": None,
                "calculation_output_metric_key": None,
            }
        }
    )
    assert "trusted_calculation_dependency_role_missing" in _validate(
        execution_plan, calculation, context
    )


def test_rogue_extra_dependency_fetch_is_denied() -> None:
    rogue_fetch = FetchMetricStep(
        step_id="fetch_rogue",
        metric_keys=("metric.stores",),
        calculation_input_role="denominator",
        calculation_binding_checksum=_binding().checksum,
        calculation_output_metric_key="metric.revenue_ratio",
    )
    execution_plan, calculation, context = _build(extra=(rogue_fetch,))
    assert "trusted_calculation_dependency_rogue" in _validate(
        execution_plan, calculation, context
    )


def test_extra_calculation_input_role_is_denied() -> None:
    surplus = FetchMetricStep(
        step_id="fetch_surplus",
        metric_keys=("metric.revenue",),
        calculation_input_role="surplus",
        calculation_binding_checksum=_binding().checksum,
        calculation_output_metric_key="metric.revenue_ratio",
    )
    execution_plan, calculation, context = _build(
        extra=(surplus,),
        depends_on=("fetch_numerator", "fetch_denominator", "fetch_surplus"),
        refs={
            "numerator": "fetch_numerator.value",
            "denominator": "fetch_denominator.value",
            "surplus": "fetch_surplus.value",
        },
    )
    assert "trusted_calculation_input_role_mismatch" in _validate(
        execution_plan, calculation, context
    )


def test_direct_fetch_of_the_canonical_output_is_denied() -> None:
    direct = FetchMetricStep(step_id="fetch_metrics", metric_keys=("metric.revenue_ratio",))
    execution_plan, calculation, context = _build(extra=(direct,))
    assert "trusted_calculation_output_fetch_conflict" in _validate(
        execution_plan, calculation, context
    )


def _route_budget() -> RouteBudget:
    return RouteBudget(
        deadline_ms=1000,
        max_model_calls=0,
        max_sql_candidates=8,
        max_sql_executions=8,
        max_join_hops=4,
        max_repairs=0,
    )


def test_compiler_emits_dependency_fetches_not_the_derived_output() -> None:
    plan, context = _plan(), _context()
    catalog = ApprovedCalculationCatalog([_binding()])
    bound = PlanCompiler(calculation_catalog=catalog).compile(
        plan=plan, context=context, validation=_allow(plan, context)
    )
    assert [s.kind for s in bound.steps] == [
        "fetch_metric",
        "fetch_metric",
        "trusted_calculation",
        "verify",
    ]
    numerator, denominator, calc, verify = bound.steps
    assert isinstance(numerator, FetchMetricStep)
    assert isinstance(denominator, FetchMetricStep)
    assert isinstance(calc, TrustedCalculationStep)
    assert isinstance(verify, VerifyStep)
    assert numerator.step_id == "fetch_numerator"
    assert numerator.metric_keys == ("metric.revenue",)
    assert numerator.calculation_input_role == "numerator"
    assert numerator.calculation_binding_checksum == _binding().checksum
    assert numerator.calculation_output_metric_key == "metric.revenue_ratio"
    assert denominator.step_id == "fetch_denominator"
    assert denominator.metric_keys == ("metric.stores",)
    # The derived output metric is NEVER fetched as a metric rowset.
    assert all(
        "metric.revenue_ratio" not in s.metric_keys
        for s in bound.steps
        if isinstance(s, FetchMetricStep)
    )
    assert calc.binding_checksum == _binding().checksum
    assert calc.output_metric_key == "metric.revenue_ratio"
    assert calc.input_refs == {
        "numerator": "fetch_numerator.value",
        "denominator": "fetch_denominator.value",
    }
    assert calc.depends_on == ("fetch_numerator", "fetch_denominator")
    assert verify.depends_on == (calc.step_id,)

    unbound = PlanCompiler().compile(plan=plan, context=context, validation=_allow(plan, context))
    assert [s.kind for s in unbound.steps] == ["fetch_metric", "verify"]
    assert not any(isinstance(s, TrustedCalculationStep) for s in unbound.steps)


def test_bound_metric_outside_metric_intent_fails_closed() -> None:
    plan = _plan().model_copy(update={"intent": "trend"})
    context = _context()
    with pytest.raises(PlanCompilationError, match="approved_calculation_intent_unsupported"):
        PlanCompiler(calculation_catalog=ApprovedCalculationCatalog([_binding()])).compile(
            plan=plan, context=context, validation=_allow(plan, context)
        )


def test_compiled_bound_plan_validates_against_its_dependency_fetches() -> None:
    plan, context = _plan(), _dependency_context()
    catalog = ApprovedCalculationCatalog([_binding()])
    execution_plan = PlanCompiler(calculation_catalog=catalog).compile(
        plan=plan, context=context, validation=_allow(plan, context)
    )
    record = PlanValidator(calculation_catalog=catalog).validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=_route_budget(),
    )
    assert record.outcome == "allow"
    assert record.issues == ()


def test_canonical_provenance_without_a_catalog_is_denied_not_approved() -> None:
    plan, context = _plan(), _dependency_context()
    catalog = ApprovedCalculationCatalog([_binding()])
    execution_plan = PlanCompiler(calculation_catalog=catalog).compile(
        plan=plan, context=context, validation=_allow(plan, context)
    )
    for validator in (
        PlanValidator(),
        # A template-id allowlist is NOT canonical metric authority.
        PlanValidator(approved_template_ids=frozenset({"ratio"})),
    ):
        record = validator.validate_execution_plan(
            execution_plan=execution_plan,
            query_plan=plan,
            context=context,
            route_budget=_route_budget(),
        )
        assert record.outcome == "deny"
        assert [issue.code for issue in record.issues] == [
            "trusted_calculation_binding_authority_missing"
        ]


def _legacy_execution_plan() -> tuple[ExecutionPlan, ContextBundle]:
    plan, context = _plan(("metric.revenue",)), _context()
    fetch = FetchMetricStep(step_id="fetch_metrics", metric_keys=plan.metric_keys)
    calculation = TrustedCalculationStep(
        step_id="calculate_metric",
        template_id="ratio",
        input_refs={"numerator": "fetch_metrics.rows", "denominator": "fetch_metrics.rows"},
        depends_on=(fetch.step_id,),
    )
    verify = VerifyStep(
        step_id="verify_result",
        input_refs=(calculation.step_id,),
        invariant_ids=("typed_result_present",),
        depends_on=(calculation.step_id,),
    )
    return (
        ExecutionPlan(
            query_plan_sha256=plan.checksum,
            semantic_release_id=context.semantic_release_id,
            schema_snapshot_id=context.schema_snapshot_id,
            policy_version="plan-compiler.test.v1",
            steps=(fetch, calculation, verify),
        ),
        context,
    )


def test_legacy_provenance_free_step_keeps_the_template_allowlist() -> None:
    execution_plan, context = _legacy_execution_plan()
    plan = _plan(("metric.revenue",))
    unapproved = PlanValidator().validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=_route_budget(),
    )
    assert unapproved.outcome == "approval"
    assert [issue.code for issue in unapproved.issues] == [
        "trusted_calculation_approval_required"
    ]
    approved = PlanValidator(approved_template_ids=frozenset({"ratio"})).validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=_route_budget(),
    )
    assert approved.outcome == "allow"
    # The mere presence of a catalog never promotes a provenance-free step.
    with_catalog = PlanValidator(
        approved_template_ids=frozenset({"ratio"}),
        calculation_catalog=_canonical_catalog(),
    ).validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=_route_budget(),
    )
    assert with_catalog.outcome == "allow"


def test_single_unbound_metric_still_compiles_an_ordinary_fetch() -> None:
    plan, context = _plan(("metric.stores",)), _dependency_context()
    execution_plan = PlanCompiler(calculation_catalog=_canonical_catalog()).compile(
        plan=plan, context=context, validation=_allow(plan, context)
    )
    assert [step.kind for step in execution_plan.steps] == ["fetch_metric", "verify"]
    first = execution_plan.steps[0]
    assert isinstance(first, FetchMetricStep)
    assert first.metric_keys == ("metric.stores",)


def test_single_bound_metric_compiles_the_canonical_dag() -> None:
    plan, context = _plan(), _dependency_context()
    execution_plan = PlanCompiler(calculation_catalog=_canonical_catalog()).compile(
        plan=plan, context=context, validation=_allow(plan, context)
    )
    assert [step.kind for step in execution_plan.steps] == [
        "fetch_metric",
        "fetch_metric",
        "trusted_calculation",
        "verify",
    ]


def test_multi_metric_plan_with_one_bound_metric_fails_closed() -> None:
    plan, context = _plan(("metric.revenue_ratio", "metric.stores")), _dependency_context()
    with pytest.raises(
        PlanCompilationError, match="approved_calculation_plan_shape_unsupported"
    ):
        PlanCompiler(calculation_catalog=_canonical_catalog()).compile(
            plan=plan, context=context, validation=_allow(plan, context)
        )


def test_multi_metric_plan_with_two_bound_metrics_fails_closed() -> None:
    other = _binding(canonical_metric_key="metric.other_ratio")
    catalog = ApprovedCalculationCatalog([_binding(), other])
    plan = _plan(("metric.revenue_ratio", "metric.other_ratio"))
    context = _dependency_context()
    with pytest.raises(
        PlanCompilationError, match="approved_calculation_plan_shape_unsupported"
    ):
        PlanCompiler(calculation_catalog=catalog).compile(
            plan=plan, context=context, validation=_allow(plan, context)
        )


def test_fetch_only_plan_for_a_bound_metric_is_denied() -> None:
    plan, context = _plan(), _dependency_context()
    fetch = FetchMetricStep(step_id="fetch_metrics", metric_keys=("metric.revenue_ratio",))
    verify = VerifyStep(
        step_id="verify_result",
        input_refs=(fetch.step_id,),
        invariant_ids=("typed_result_present",),
        depends_on=(fetch.step_id,),
    )
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(fetch, verify),
    )
    record = _validate_plan(execution_plan, plan, context)
    assert record.outcome == "deny"
    codes = [issue.code for issue in record.issues]
    assert "trusted_calculation_required_for_bound_metric" in codes
    assert "trusted_calculation_output_fetch_conflict" in codes


def test_bound_metric_without_any_calculation_is_denied() -> None:
    plan, context = _plan(), _dependency_context()
    fetch = FetchMetricStep(step_id="fetch_metrics", metric_keys=("metric.revenue",))
    verify = VerifyStep(
        step_id="verify_result",
        input_refs=(fetch.step_id,),
        invariant_ids=("typed_result_present",),
        depends_on=(fetch.step_id,),
    )
    execution_plan = ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=context.semantic_release_id,
        schema_snapshot_id=context.schema_snapshot_id,
        policy_version="plan-compiler.test.v1",
        steps=(fetch, verify),
    )
    record = _validate_plan(execution_plan, plan, context)
    assert record.outcome == "deny"
    assert [issue.code for issue in record.issues] == [
        "trusted_calculation_required_for_bound_metric"
    ]


def test_direct_fetch_alongside_a_valid_calculation_is_denied() -> None:
    plan = _plan()
    execution_plan, _, context = _build()
    direct = FetchMetricStep(step_id="fetch_direct", metric_keys=("metric.revenue_ratio",))
    record = _validate_plan(_with_steps(execution_plan, direct), plan, context)
    assert record.outcome == "deny"
    assert "trusted_calculation_output_fetch_conflict" in [
        issue.code for issue in record.issues
    ]


def test_two_canonical_calculations_for_one_output_are_denied() -> None:
    plan = _plan()
    first, _, context = _build()
    second, _, _ = _build(suffix="_two")
    combined = ExecutionPlan(
        query_plan_sha256=first.query_plan_sha256,
        semantic_release_id=first.semantic_release_id,
        schema_snapshot_id=first.schema_snapshot_id,
        policy_version=first.policy_version,
        steps=(*first.steps[:-1], *second.steps),
    )
    record = _validate_plan(combined, plan, context)
    assert record.outcome == "deny"
    assert [issue.code for issue in record.issues] == [
        "trusted_calculation_output_duplicate"
    ]


def test_exactly_one_valid_canonical_calculation_is_allowed() -> None:
    plan, context = _plan(), _dependency_context()
    execution_plan = PlanCompiler(calculation_catalog=_canonical_catalog()).compile(
        plan=plan, context=context, validation=_allow(plan, context)
    )
    record = _validate_plan(execution_plan, plan, context)
    assert record.outcome == "allow"
    assert record.issues == ()


def test_legacy_calculation_cannot_launder_an_orphan_dependency_fetch() -> None:
    execution_plan, calculation, context = _build()
    orphan = FetchMetricStep(
        step_id="fetch_orphan",
        metric_keys=("metric.stores",),
        calculation_input_role="denominator",
        calculation_binding_checksum=_binding().checksum,
        calculation_output_metric_key="metric.revenue_ratio",
    )
    legacy = TrustedCalculationStep(
        step_id="calculate_legacy",
        template_id="ratio",
        input_refs={
            "numerator": "fetch_numerator.value",
            "denominator": "fetch_numerator.value",
        },
        depends_on=("fetch_numerator", "fetch_orphan"),
    )
    combined = _with_steps(execution_plan, orphan, legacy)
    assert "trusted_calculation_dependency_rogue" in _validate(
        combined, calculation, context
    )


def test_legacy_provenance_free_calculation_keeps_its_own_allowlist_behavior() -> None:
    plan = _plan()
    execution_plan, _, context = _build()
    legacy = TrustedCalculationStep(
        step_id="calculate_legacy",
        template_id="ratio",
        input_refs={
            "numerator": "fetch_numerator.value",
            "denominator": "fetch_numerator.value",
        },
        depends_on=("fetch_numerator",),
    )
    combined = _with_steps(execution_plan, legacy)
    unapproved = _validate_plan(combined, plan, context)
    assert unapproved.outcome == "approval"
    assert [issue.code for issue in unapproved.issues] == [
        "trusted_calculation_approval_required"
    ]
    approved = PlanValidator(
        approved_template_ids=frozenset({"ratio"}),
        calculation_catalog=_canonical_catalog(),
    ).validate_execution_plan(
        execution_plan=combined,
        query_plan=plan,
        context=context,
        route_budget=_route_budget(),
    )
    assert approved.outcome == "allow"


def _nested_catalog() -> ApprovedCalculationCatalog:
    """C = ratio(revenue, stores) where revenue is ITSELF catalog-bound."""

    return ApprovedCalculationCatalog(
        [_binding(), _binding(canonical_metric_key="metric.revenue")]
    )


def _rebuild(
    execution_plan: ExecutionPlan,
    plan: QueryPlan,
    steps: tuple[FetchMetricStep | TrustedCalculationStep | VerifyStep, ...],
) -> ExecutionPlan:
    return ExecutionPlan(
        query_plan_sha256=plan.checksum,
        semantic_release_id=execution_plan.semantic_release_id,
        schema_snapshot_id=execution_plan.schema_snapshot_id,
        policy_version=execution_plan.policy_version,
        steps=steps,
    )


def _canonical_calculation(execution_plan: ExecutionPlan) -> TrustedCalculationStep:
    return next(
        step
        for step in execution_plan.steps
        if isinstance(step, TrustedCalculationStep)
    )


def _provenance_fetches(execution_plan: ExecutionPlan) -> tuple[FetchMetricStep, ...]:
    return tuple(
        step
        for step in execution_plan.steps
        if isinstance(step, FetchMetricStep) and step.calculation_input_role is not None
    )


def test_self_referential_binding_cannot_produce_an_executable_plan() -> None:
    self_ref = _binding(
        inputs=(
            ApprovedCalculationInput(
                role="numerator", metric_key="metric.revenue_ratio", metric_contract_sha256=MCS
            ),
            ApprovedCalculationInput(
                role="denominator", metric_key="metric.stores", metric_contract_sha256=MCS
            ),
        )
    )
    catalog = ApprovedCalculationCatalog([self_ref])
    plan, context = _plan(), _dependency_context()
    execution_plan = PlanCompiler(calculation_catalog=catalog).compile(
        plan=plan, context=context, validation=_allow(plan, context)
    )
    record = _validate_plan(execution_plan, plan, context, catalog=catalog)
    assert record.outcome == "deny"
    assert "trusted_calculation_nested_dependency_unsupported" in [
        issue.code for issue in record.issues
    ]


def test_nested_catalog_binding_is_denied_by_the_validator() -> None:
    plan = _plan()
    execution_plan, _, context = _build()
    record = _validate_plan(execution_plan, plan, context, catalog=_nested_catalog())
    assert record.outcome == "deny"
    assert "trusted_calculation_nested_dependency_unsupported" in [
        issue.code for issue in record.issues
    ]


async def test_runner_refuses_a_catalog_bound_dependency() -> None:
    runner = GatewayMetricStepRunner(
        _RecordingCompiler(), _FakeGateway(), calculation_catalog=_nested_catalog()
    )
    with pytest.raises(PlanStepError, match="metric_dependency_nested_calculation_unsupported"):
        await runner.prepare(
            step=_numerator_step(), query_plan=_plan(), context=_dependency_context()
        )


async def test_runner_still_accepts_an_unbound_dependency() -> None:
    compiler = _RecordingCompiler()
    runner = GatewayMetricStepRunner(
        compiler, _FakeGateway(), calculation_catalog=_canonical_catalog()
    )
    prepared = await runner.prepare(
        step=_numerator_step(), query_plan=_plan(), context=_dependency_context()
    )
    assert prepared.dependency_fetch is True
    assert compiler.plans[0].metric_keys == ("metric.revenue",)


def test_orphan_provenance_fetch_is_plan_invalid() -> None:
    plan = _plan(("metric.revenue",))
    execution_plan, _, context = _build()
    orphaned = _rebuild(execution_plan, plan, _provenance_fetches(execution_plan))
    record = _validate_plan(orphaned, plan, context)
    assert record.outcome == "deny"
    assert "trusted_calculation_dependency_rogue" in [
        issue.code for issue in record.issues
    ]


def test_legacy_calculation_is_not_a_canonical_consumer() -> None:
    plan = _plan(("metric.revenue",))
    execution_plan, _, context = _build()
    fetches = _provenance_fetches(execution_plan)
    legacy = TrustedCalculationStep(
        step_id="calculate_legacy",
        template_id="ratio",
        input_refs={
            "numerator": "fetch_numerator.value",
            "denominator": "fetch_denominator.value",
        },
        depends_on=tuple(step.step_id for step in fetches),
    )
    rebuilt = _rebuild(execution_plan, plan, (*fetches, legacy))
    record = _validate_plan(rebuilt, plan, context)
    assert record.outcome == "deny"
    assert "trusted_calculation_dependency_rogue" in [
        issue.code for issue in record.issues
    ]


def test_two_canonical_calculations_cannot_share_one_dependency_fetch() -> None:
    plan = _plan()
    first, _, context = _build()
    second, _, _ = _build(suffix="_two")
    fetches = _provenance_fetches(first)
    calc_one = _canonical_calculation(first)
    calc_two = _canonical_calculation(second).model_copy(
        update={
            "depends_on": tuple(step.step_id for step in fetches),
            "input_refs": {
                "numerator": "fetch_numerator.value",
                "denominator": "fetch_denominator.value",
            },
        }
    )
    rebuilt = _rebuild(first, plan, (*fetches, calc_one, calc_two))
    record = _validate_plan(rebuilt, plan, context)
    assert record.outcome == "deny"
    codes = [issue.code for issue in record.issues]
    assert "trusted_calculation_dependency_duplicate_consumer" in codes


def test_dependency_fetch_role_must_match_its_canonical_consumer() -> None:
    plan = _plan()
    execution_plan, _, context = _build()
    calc = _canonical_calculation(execution_plan)
    broken = calc.model_copy(
        update={
            "input_refs": {
                "numerator": "fetch_denominator.value",
                "denominator": "fetch_denominator.value",
            }
        }
    )
    steps = tuple(broken if step is calc else step for step in execution_plan.steps)
    rebuilt = _rebuild(execution_plan, plan, steps)
    record = _validate_plan(rebuilt, plan, context)
    assert record.outcome == "deny"
    assert "trusted_calculation_input_ref_mismatch" in [
        issue.code for issue in record.issues
    ]


def test_provenance_fetch_without_a_catalog_is_plan_invalid() -> None:
    plan = _plan(("metric.revenue",))
    execution_plan, _, context = _build()
    orphaned = _rebuild(execution_plan, plan, _provenance_fetches(execution_plan))
    record = PlanValidator().validate_execution_plan(
        execution_plan=orphaned,
        query_plan=plan,
        context=context,
        route_budget=_route_budget(),
    )
    assert record.outcome == "deny"
    assert [issue.code for issue in record.issues] == [
        "trusted_calculation_binding_authority_missing"
    ]


def test_ordinary_compiler_produced_calculation_still_validates() -> None:
    plan, context = _plan(), _dependency_context()
    catalog = _canonical_catalog()
    execution_plan = PlanCompiler(calculation_catalog=catalog).compile(
        plan=plan, context=context, validation=_allow(plan, context)
    )
    record = _validate_plan(execution_plan, plan, context, catalog=catalog)
    assert record.outcome == "allow"
    assert record.issues == ()


def test_unrequested_canonical_calculation_is_denied() -> None:
    plan = _plan(("metric.revenue",))
    execution_plan, _, context = _build()
    rebuilt = _rebuild(execution_plan, plan, execution_plan.steps)
    record = _validate_plan(rebuilt, plan, context)
    assert record.outcome == "deny"
    assert [issue.code for issue in record.issues] == [
        "trusted_calculation_output_not_requested"
    ]


def test_requested_canonical_output_with_one_calculation_is_allowed() -> None:
    plan, context = _plan(), _dependency_context()
    execution_plan, _, _ = _build()
    record = _validate_plan(execution_plan, plan, context)
    assert record.outcome == "allow"
    assert record.issues == ()


def test_requested_canonical_output_with_an_unbound_metric_is_allowed() -> None:
    plan = _plan(("metric.revenue_ratio", "metric.other"))
    execution_plan, _, context = _build()
    extra = FetchMetricStep(step_id="fetch_other", metric_keys=("metric.other",))
    rebuilt = _rebuild(execution_plan, plan, (*execution_plan.steps, extra))
    record = _validate_plan(rebuilt, plan, context)
    assert record.outcome == "allow"
    assert record.issues == ()


def test_legacy_calculation_is_unaffected_by_requested_output_closure() -> None:
    execution_plan, context = _legacy_execution_plan()
    plan = _plan(("metric.revenue",))
    record = PlanValidator(calculation_catalog=_canonical_catalog()).validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=_route_budget(),
    )
    assert "trusted_calculation_output_not_requested" not in [
        issue.code for issue in record.issues
    ]
    assert record.outcome == "approval"


class _PreparedSql:
    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint


class _FakeGateway:
    def prepare(self, sql: str) -> _PreparedSql:
        del sql
        return _PreparedSql("f" * 64)


class _RecordingCompiler:
    def __init__(self) -> None:
        self.plans: list[QueryPlan] = []

    async def compile(self, plan: QueryPlan, context: ContextBundle) -> CompiledMetricQuery:
        self.plans.append(plan)
        return CompiledMetricQuery(
            sql="SELECT synthetic",
            params={},
            release_id=str(context.semantic_release_id),
            snapshot_id=str(context.schema_snapshot_id),
            snapshot_checksum="0" * 64,
            query_plan=plan,
            context=context,
        )


def _numerator_step(**overrides: object) -> FetchMetricStep:
    base: dict[str, object] = {
        "step_id": "fetch_numerator",
        "metric_keys": ("metric.revenue",),
        "calculation_input_role": "numerator",
        "calculation_binding_checksum": _binding().checksum,
        "calculation_output_metric_key": "metric.revenue_ratio",
    }
    base.update(overrides)
    return FetchMetricStep(**base)


async def test_dependency_fetch_prepare_derives_one_child_plan() -> None:
    compiler = _RecordingCompiler()
    runner = GatewayMetricStepRunner(
        compiler, _FakeGateway(), calculation_catalog=ApprovedCalculationCatalog([_binding()])
    )
    prepared = await runner.prepare(
        step=_numerator_step(), query_plan=_plan(), context=_dependency_context()
    )
    assert prepared.dependency_fetch is True
    child = compiler.plans[0]
    assert child.metric_keys == ("metric.revenue",)
    assert child.intent == "metric"
    assert child.domain == "finance"
    assert child.time_range == _plan().time_range
    assert child.grain == _plan().grain
    assert child.required_permissions == _plan().required_permissions


async def test_direct_fetch_prepare_is_unchanged() -> None:
    compiler = _RecordingCompiler()
    runner = GatewayMetricStepRunner(compiler, _FakeGateway())
    plan = _plan()
    prepared = await runner.prepare(
        step=FetchMetricStep(step_id="fetch_metrics", metric_keys=plan.metric_keys),
        query_plan=plan,
        context=_dependency_context(),
    )
    assert prepared.dependency_fetch is False
    assert compiler.plans[0] is plan


async def test_dependency_fetch_without_a_catalog_fails_closed() -> None:
    runner = GatewayMetricStepRunner(_RecordingCompiler(), _FakeGateway())
    with pytest.raises(PlanStepError, match="metric_dependency_binding_missing"):
        await runner.prepare(
            step=_numerator_step(), query_plan=_plan(), context=_dependency_context()
        )


@pytest.mark.parametrize(
    "overrides,code",
    [
        (
            {"calculation_binding_checksum": "d" * 64},
            "metric_dependency_binding_mismatch",
        ),
        ({"calculation_input_role": "nope"}, "metric_dependency_role_mismatch"),
        ({"metric_keys": ("metric.stores",)}, "metric_dependency_metric_mismatch"),
        (
            {"calculation_output_metric_key": "metric.other"},
            "metric_dependency_binding_missing",
        ),
    ],
)
async def test_dependency_fetch_prepare_fails_closed(
    overrides: dict[str, object], code: str
) -> None:
    runner = GatewayMetricStepRunner(
        _RecordingCompiler(),
        _FakeGateway(),
        calculation_catalog=ApprovedCalculationCatalog([_binding()]),
    )
    with pytest.raises(PlanStepError, match=code):
        await runner.prepare(
            step=_numerator_step(**overrides),
            query_plan=_plan(),
            context=_dependency_context(),
        )


async def test_dependency_fetch_requires_a_resolved_metric() -> None:
    runner = GatewayMetricStepRunner(
        _RecordingCompiler(),
        _FakeGateway(),
        calculation_catalog=ApprovedCalculationCatalog([_binding()]),
    )
    with pytest.raises(PlanStepError, match="metric_dependency_metric_unresolved"):
        await runner.prepare(
            step=_numerator_step(), query_plan=_plan(), context=_context()
        )


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([{"value": 3}], 3),
        ([{"value": 2.5}], 2.5),
        ([{"value": "2.00", "status": "success"}], "2.00"),
    ],
)
def test_dependency_scalar_projection(rows: list[object], expected: object) -> None:
    assert project_dependency_scalar(rows, no_data=False) == expected


@pytest.mark.parametrize(
    "rows,no_data,code",
    [
        ([{"value": 1}], True, "metric_dependency_no_data"),
        ([], False, "metric_dependency_scalar_required"),
        ([{"value": 1}, {"value": 2}], False, "metric_dependency_scalar_required"),
        ([{"value": None}], False, "metric_dependency_no_data"),
        (
            [{"numerator": 1, "value": 1, "status": "no_data"}],
            False,
            "metric_dependency_no_data",
        ),
        ([{"value": 1, "status": "weird"}], False, "metric_dependency_status_invalid"),
        ([{"numerator": 1}], False, "metric_dependency_value_missing"),
        ([{"value": {"nested": 1}}], False, "metric_dependency_scalar_required"),
    ],
)
def test_dependency_scalar_projection_fails_closed(
    rows: list[object], no_data: bool, code: str
) -> None:
    with pytest.raises(PlanStepError, match=code):
        project_dependency_scalar(rows, no_data=no_data)


def test_dependency_fetch_provenance_is_all_or_none() -> None:
    with pytest.raises(ValueError):
        _numerator_step(calculation_binding_checksum=None)
    with pytest.raises(ValueError):
        _numerator_step(metric_keys=("metric.revenue", "metric.stores"))


def test_calculation_receipt_grounds_a_canonical_answer_fact() -> None:
    plan, context = _plan(), _dependency_context()
    ex_plan = PlanCompiler(calculation_catalog=ApprovedCalculationCatalog([_binding()])).compile(
        plan=plan, context=context, validation=_allow(plan, context)
    )
    record = PlanExecutionRecord(
        execution_plan_checksum=ex_plan.checksum,
        status="succeeded",
        step_receipts=(
            PlanStepReceipt(step_id="fetch_numerator", kind="fetch_metric", status="succeeded", elapsed_ms=1, output_digest="1" * 64),
            PlanStepReceipt(step_id="fetch_denominator", kind="fetch_metric", status="succeeded", elapsed_ms=1, output_digest="2" * 64),
            PlanStepReceipt(
                step_id="calculate_metric",
                kind="trusted_calculation",
                status="succeeded",
                elapsed_ms=1,
                output_digest="f" * 64,
                template_id="ratio",
                template_version="1.0",
                binding_checksum=_binding().checksum,
                output_metric_key="metric.revenue_ratio",
            ),
            PlanStepReceipt(step_id="verify_result", kind="verify", status="succeeded", elapsed_ms=1, output_digest="a" * 64),
        ),
        output_step_ids=(
            "fetch_numerator",
            "fetch_denominator",
            "calculate_metric",
            "verify_result",
        ),
    )
    facts = build_answer_facts(
        query_plan=plan,
        execution_plan=ex_plan,
        record=record,
        outputs={
            "fetch_numerator": {"value": 100},
            "fetch_denominator": {"value": 50.0},
            "calculate_metric": {"ratio": 2.0},
            "verify_result": {"ok": True},
        },
    )
    # Exactly ONE fact per requested metric: the internal dependency fetches
    # ground nothing on their own, so the requested value has a single authority.
    assert [f.metric_key for f in facts] == ["metric.revenue_ratio"]
    calc_facts = [f for f in facts if f.metric_key == "metric.revenue_ratio"]
    assert len(calc_facts) == 1
    assert calc_facts[0].step_id == "calculate_metric"
    assert calc_facts[0].status == "grounded"
    assert calc_facts[0].value == 2.0
    assert calc_facts[0].output_digest == "f" * 64
    assert not [f for f in facts if f.step_id == "verify_result"]
