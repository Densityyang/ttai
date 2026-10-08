"""Production runner injection gate for QUERY run-scoped AD_HOC calculation."""

from __future__ import annotations

import hashlib
import inspect
from datetime import date
from typing import Any, cast
from uuid import UUID

import pytest

from src.nl2sql.contracts import (
    AdHocCalculationStep,
    ContextBundle,
    ExecutionPlan,
    ExecutionReceipt,
    FetchMetricStep,
    QueryPlan,
    TimeRange,
    VerifyStep,
)
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
    RuntimeCalculationRunner,
)
from src.nl2sql.orchestration.metric_query import metric_plan_executor
from src.nl2sql.orchestration.planning import PlanValidator
from src.nl2sql.orchestration.typed_runtime import (
    RequestTypedRuntime,
    ad_hoc_calculation_runner_for_capabilities,
    build_request_typed_runtime,
)
from src.nl2sql.semantic.calculation_contract import (
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    InputRefOperand,
    derived_output_id,
)
from tests.unit.test_typed_runtime import (
    _authorization,
    _deployment,
    _gateway,
    _identity,
    _views,
)

RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
_EXPECTED_POLICY = PlanValidator()


def _spec() -> CalculationSpec:
    return CalculationSpec(
        calculation_id="adhoc.revenue_per_store",
        expression=BinaryOperand(
            op="divide",
            left=InputRefOperand(role="numerator"),
            right=InputRefOperand(role="denominator"),
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


def _context() -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE_ID,
        schema_snapshot_id=SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=("metric.revenue", "metric.stores"),
        resolution_status="resolved",
    )


def _adhoc_execution_plan(
    spec: CalculationSpec, binding: CalculationExecutionBinding, query_plan: QueryPlan, context: ContextBundle
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

    async def prepare(
        self, *, step: FetchMetricStep, query_plan: QueryPlan, context: ContextBundle
    ) -> PreparedMetricStep:
        del query_plan, context
        fingerprint = hashlib.sha256(step.metric_keys[0].encode()).hexdigest()
        return PreparedMetricStep(
            sql_fingerprint=fingerprint,
            join_hops=0,
            payload={"metric": step.metric_keys[0], "fingerprint": fingerprint},
        )

    async def execute(
        self, prepared: PreparedMetricStep, *, timeout_ms: int
    ) -> MetricStepResult:
        del timeout_ms
        metric = cast(dict[str, Any], prepared.payload)["metric"]
        return MetricStepResult(
            value={"value": self.values[metric]},
            receipt=ExecutionReceipt(
                datasource="synthetic",
                readonly_role="fixture_reader",
                elapsed_ms=1,
                row_count=1,
                sql_fingerprint=prepared.sql_fingerprint,
                policy_version="query-gateway.test.v1",
                policy_outcome="allow",
            ),
        )


# --- the gate -----------------------------------------------------------------


def test_metric_plan_executor_defaults_to_no_ad_hoc_runner() -> None:
    executor = metric_plan_executor(cast(Any, object()), cast(Any, object()))
    assert executor._ad_hoc_calculation_runner is None


def test_metric_plan_executor_injects_an_explicit_ad_hoc_runner() -> None:
    runner = RuntimeCalculationRunner()
    executor = metric_plan_executor(
        cast(Any, object()), cast(Any, object()), ad_hoc_calculation_runner=runner
    )
    assert executor._ad_hoc_calculation_runner is runner


def test_capability_gate_is_explicit_and_fail_closed() -> None:
    assert ad_hoc_calculation_runner_for_capabilities(frozenset()) is None
    assert (
        ad_hoc_calculation_runner_for_capabilities(frozenset({"model_analysis"})) is None
    )
    granted = ad_hoc_calculation_runner_for_capabilities(
        frozenset({"run_scoped_derivation"})
    )
    assert isinstance(granted, RuntimeCalculationRunner)


def test_build_request_typed_runtime_defaults_capabilities_to_fail_closed() -> None:
    signature = inspect.signature(build_request_typed_runtime)
    default = signature.parameters["capabilities"].default
    assert default == frozenset()
    assert "run_scoped_derivation" not in default


# --- end-to-end gate on the request-scoped typed runtime -----------------------


@pytest.mark.asyncio
async def test_typed_runtime_keeps_ad_hoc_fail_closed_without_the_capability() -> None:
    deployment = _deployment()
    runtime = await build_request_typed_runtime(
        views=_views(),
        read_active=deployment.read_active,
        read_snapshot=deployment.read_snapshot,
        gateway=_gateway(),
        identity=_identity(),
        authorization=_authorization(),
    )
    assert isinstance(runtime, RequestTypedRuntime)
    assert runtime.plan_executor._ad_hoc_calculation_runner is None


@pytest.mark.asyncio
async def test_typed_runtime_injects_the_shared_runner_when_capability_is_granted() -> None:
    deployment = _deployment()
    runtime = await build_request_typed_runtime(
        views=_views(),
        read_active=deployment.read_active,
        read_snapshot=deployment.read_snapshot,
        gateway=_gateway(),
        identity=_identity(),
        authorization=_authorization(),
        capabilities=frozenset({"run_scoped_derivation"}),
    )
    assert isinstance(runtime, RequestTypedRuntime)
    assert isinstance(
        runtime.plan_executor._ad_hoc_calculation_runner, RuntimeCalculationRunner
    )


# --- unauthorized executor still fails closed at execution time ----------------


@pytest.mark.asyncio
async def test_unauthorized_runner_value_fails_closed_at_execution() -> None:
    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _adhoc_execution_plan(spec, binding, query_plan, context)
    unauthorized = metric_plan_executor(
        cast(Any, object()), cast(Any, object())
    )._ad_hoc_calculation_runner
    assert unauthorized is None
    result = await PlanExecutor(
        metric_runner=_MetricRunner({"metric.revenue": 5, "metric.stores": 2}),
        ad_hoc_calculation_runner=unauthorized,
    ).execute(
        query_plan=query_plan,
        context=context,
        execution_plan=plan,
        validation=PlanValidator().validate_execution_plan(
            execution_plan=plan,
            query_plan=query_plan,
            context=context,
            route_budget=RouteBudgetLedger(route="standard").limits,
        ),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=RouteBudgetLedger(route="standard"),
        deadline_ms=4_000,
    )
    assert result.record.status == "failed"
    assert result.record.stop_reason == "ad_hoc_calculation_unavailable"
    assert "calculate_adhoc" not in result.outputs


@pytest.mark.asyncio
async def test_authorized_runner_executes_without_creating_a_definition() -> None:
    """A4 boundary: the AD_HOC execution path produces no Custom Definition."""

    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _adhoc_execution_plan(spec, binding, query_plan, context)
    result = await PlanExecutor(
        metric_runner=_MetricRunner({"metric.revenue": 5, "metric.stores": 2}),
        ad_hoc_calculation_runner=ad_hoc_calculation_runner_for_capabilities(
            frozenset({"run_scoped_derivation"})
        ),
    ).execute(
        query_plan=query_plan,
        context=context,
        execution_plan=plan,
        validation=PlanValidator().validate_execution_plan(
            execution_plan=plan,
            query_plan=query_plan,
            context=context,
            route_budget=RouteBudgetLedger(route="standard").limits,
        ),
        expected_policy_version=_EXPECTED_POLICY.policy_version,
        expected_policy_checksum=_EXPECTED_POLICY.policy_checksum,
        budget=RouteBudgetLedger(route="standard"),
        deadline_ms=4_000,
    )
    assert result.record.status == "succeeded"
    # The executor has exactly four registered collaborators and none of them is
    # a definition/repository/persistence port.
    assert set(vars(PlanExecutor(
        metric_runner=_MetricRunner({}),
    ))) == {
        "_metric_runner",
        "_trusted_calculation_runner",
        "_ad_hoc_calculation_runner",
        "_result_verifier",
    }
