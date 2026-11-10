"""QUERY AD_HOC noncanonical execution carrier (contracts + executor + grounding)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError

from src.nl2sql.agents.dynamic_calc.trusted_templates import trusted_template_registry
from src.nl2sql.contracts import (
    AdHocCalculationStep,
    AnswerArtifact,
    AnswerFact,
    AuthorizationContext,
    ContextBundle,
    ExecutionPlan,
    ExecutionReceipt,
    FetchMetricStep,
    PlanExecutionRecord,
    PlanStep,
    PlanStepReceipt,
    QueryPlan,
    RequestContext,
    RequestIdentity,
    RoutePolicy,
    TimeRange,
    TrustedCalculationStep,
    VerifyStep,
)
from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.analysis_evidence import (
    AnalysisEvidenceError,
    _grounded_fact_id,
    validate_grounded_answer_checkpoint,
)
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
    ApprovedCalculationInput,
)
from src.nl2sql.orchestration.budget import RouteBudgetLedger
from src.nl2sql.orchestration.checkpoint_evidence_seal import (
    seal_grounded_execution_checkpoint,
)
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.evidence_digest import canonical_json_digest
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PlanStepError,
    PreparedMetricStep,
    RegistryTrustedCalculationRunner,
    TypedResultVerifier,
    validate_execution_record_against_plan,
)
from src.nl2sql.orchestration.grounding import (
    GroundingIntegrityError,
    build_answer_facts,
    ground_execution_answer,
    render_grounded_answer,
)
from src.nl2sql.orchestration.mode_contract import RunEnvelope
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
from src.nl2sql.ownership import runtime_config
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


class _GatewayMetricRunner(_MetricRunner):
    def __init__(self, values: dict[str, float], gateway: QueryGateway) -> None:
        super().__init__(values)
        self.gateway = gateway

    async def execute(
        self,
        prepared: PreparedMetricStep,
        *,
        timeout_ms: int,
    ) -> MetricStepResult:
        await self.gateway.execute(
            "SELECT synthetic", {}, trace_id="trusted-checkpoint-test"
        )
        return await super().execute(prepared, timeout_ms=timeout_ms)


class _CountingTrustedRunner:
    def __init__(self) -> None:
        self.calls = 0
        self._inner = RegistryTrustedCalculationRunner()

    async def execute(self, *, step: TrustedCalculationStep, inputs: dict[str, Any]) -> Any:
        self.calls += 1
        return await self._inner.execute(step=step, inputs=inputs)


class _EngineContextResolver:
    def __init__(self, context: ContextBundle) -> None:
        self.context = context

    async def resolve(
        self, *, question: str, identity: RequestIdentity, route_hint: str
    ) -> ContextBundle:
        del question, identity, route_hint
        return self.context


class _EngineQueryPlanProvider:
    is_deterministic = True

    def __init__(self, plan: QueryPlan) -> None:
        self.plan = plan

    async def propose(
        self, *, question: str, context: ContextBundle, identity: RequestIdentity
    ) -> QueryPlan:
        del question, context, identity
        return self.plan


class _EngineAnalysisProvider:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        timeout_ms: int,
        max_output_tokens: int,
        response_schema: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        del model, timeout_ms, max_output_tokens, response_schema
        self.calls += 1
        source_message = messages[-1]["content"]
        source_json = source_message.split("SOURCE FACTS\n", 1)[1].split(
            "\n\nREQUESTED INTERPRETATION", 1
        )[0]
        fact = json.loads(source_json)["facts"][0]
        return ProviderResponse(
            content=json.dumps(
                {
                    "summary": {
                        "text": "The approved calculation output is recorded.",
                        "fact_ids": [fact["fact_id"]],
                    },
                    "observations": [],
                    "caveats": [],
                }
            ),
            model="controlled-analysis",
            usage={},
            finish_reason="stop",
        )


def _approved_ratio_binding(
    output_metric_key: str = "metric.revenue_ratio",
    *,
    binding_revision: str = "unit-authority-test",
) -> ApprovedCalculationBinding:
    template = trusted_template_registry.metadata("ratio")
    return ApprovedCalculationBinding(
        canonical_metric_key=output_metric_key,
        template_id=template.template_id,
        template_version=template.version,
        template_checksum=template.checksum,
        inputs=(
            ApprovedCalculationInput(
                role="numerator",
                metric_key="metric.revenue",
                metric_contract_sha256="a" * 64,
            ),
            ApprovedCalculationInput(
                role="denominator",
                metric_key="metric.stores",
                metric_contract_sha256="b" * 64,
            ),
        ),
        binding_revision=binding_revision,
        semantic_release_id=str(RELEASE_ID),
        semantic_release_checksum="c" * 64,
        unit="ratio",
    )


def _trusted_engine(
    *,
    catalog: ApprovedCalculationCatalog | None,
    checkpointer: MemorySaver,
    metric_runner: _GatewayMetricRunner,
    calculation_runner: _CountingTrustedRunner,
    provider: _EngineAnalysisProvider,
) -> Any:
    output_metric_key = "metric.revenue_ratio"
    context = _context(
        asset_ids=("metric.revenue", "metric.stores", output_metric_key)
    )
    query_plan = _query_plan().model_copy(
        update={"metric_keys": (output_metric_key,)}
    )
    validator = PlanValidator(calculation_catalog=catalog)
    compiler = PlanCompiler(calculation_catalog=catalog)
    gateway = ModelGateway(
        providers={"synthetic": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("synthetic", "small", "small"),
                None,
            )
        },
    )
    route_policy = RoutePolicy(
        version="trusted-checkpoint-standard-only",
        state="bootstrap",
        fast_max_risk=20,
        fast_min_confidence=1.0,
        fast_max_tables=1,
        standard_max_risk=60,
        standard_min_confidence=0.55,
    )
    return create_v2_engine(
        checkpointer=checkpointer,
        model_gateway=gateway,
        route_policy=route_policy,
        context_resolver=_EngineContextResolver(context),
        query_plan_provider=_EngineQueryPlanProvider(query_plan),
        plan_validator=validator,
        plan_compiler=compiler,
        plan_executor=PlanExecutor(
            metric_runner=metric_runner,
            trusted_calculation_runner=calculation_runner,
            calculation_catalog=catalog,
            allow_legacy_unitless_grounding=True,
        ),
    )


def _trusted_request_config() -> dict[str, Any]:
    identity = RequestIdentity(
        request_id=UUID("55555555-5555-5555-5555-555555555555"),
        user_id="analyst",
        roles=frozenset({"analyst"}),
        permissions=frozenset({"metrics:read", "nl2sql:invoke"}),
    )
    authorization = AuthorizationContext(
        authorization_revision="trusted-checkpoint-rev-1",
        agent_enabled=True,
        scope_level="team",
        allowed_scope_ids=("team-1",),
    )
    context = RequestContext(
        identity=identity,
        thread_id=UUID("66666666-6666-6666-6666-666666666666"),
        trace_id="trace-trusted-checkpoint",
        deadline_ms=30_000,
        authorization=authorization,
    )
    return runtime_config(context)


def _trusted_graph_input() -> dict[str, object]:
    return {
        "messages": [{"role": "user", "content": "revenue ratio"}],
        "run_envelope": RunEnvelope(
            run_id="run-trusted-checkpoint",
            requested_mode="ANALYZE",
            effective_mode="ANALYZE",
        ).model_dump(mode="json"),
    }


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
        allow_legacy_unitless=True,
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
        "output_digest": canonical_json_digest(2.5),
        "calculation_spec_checksum": spec.checksum,
        "execution_binding_checksum": binding.checksum,
        "derived_output_id": derived_output_id(spec, binding),
        "calculation_scope": "ad_hoc_noncanonical",
        "unit": spec.custom_unit_id if spec.unit == "custom_unit" else spec.unit,
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
        allow_legacy_unitless=True,
    ) == ()

    # 2. receipt pointing at a NON-AD_HOC step
    wrong_kind = _adhoc_receipt(spec, binding, step_id="fetch_numerator")
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, wrong_kind), outputs={"fetch_numerator": 2.5},
        allow_legacy_unitless=True,
    ) == ()

    # 3. mismatched derived_output_id
    bad_id = _adhoc_receipt(
        spec, binding, derived_output_id="adhoc_" + "0" * 32
    )
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, bad_id), outputs=outputs,
        allow_legacy_unitless=True,
    ) == ()

    # 4. mismatched spec checksum
    bad_spec = _adhoc_receipt(spec, binding, calculation_spec_checksum="0" * 64)
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, bad_spec), outputs=outputs,
        allow_legacy_unitless=True,
    ) == ()

    # 5. mismatched execution-binding checksum
    bad_binding = _adhoc_receipt(
        spec, binding, execution_binding_checksum="0" * 64
    )
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, bad_binding), outputs=outputs,
        allow_legacy_unitless=True,
    ) == ()

    # 7. missing request-local output -> no derived fact
    good = _adhoc_receipt(spec, binding)
    with pytest.raises(GroundingIntegrityError, match="grounding_output_missing"):
        build_answer_facts(
            query_plan=query_plan,
            execution_plan=plan,
            record=_record(plan, good),
            outputs={},
            allow_legacy_unitless=True,
        )

    # 6. the matching receipt still grounds exactly once
    facts = build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, good), outputs=outputs,
        allow_legacy_unitless=True,
    )
    assert len(facts) == 1
    assert facts[0].derived_output_id == derived_output_id(spec, binding)
    wrong_unit = _adhoc_receipt(spec, binding, unit="count")
    with pytest.raises(
        GroundingIntegrityError, match="grounding_unit_authority_mismatch"
    ):
        build_answer_facts(
            query_plan=query_plan,
            execution_plan=plan,
            record=_record(plan, wrong_unit),
            outputs=outputs,
            allow_legacy_unitless=True,
        )


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
    with pytest.raises(GroundingIntegrityError, match="execution_record_verify_missing"):
        ground_execution_answer(
            query_plan=query_plan,
            execution_plan=plan,
            record=record,
            outputs={"calculate_ratio": 2.5},
            allow_legacy_unitless=True,
        )


def test_matching_canonical_receipt_has_no_mismatch_flag() -> None:
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    query_plan, context, plan = _trusted_plan()
    record = _record(plan, _trusted_receipt())
    with pytest.raises(GroundingIntegrityError, match="execution_record_verify_missing"):
        ground_execution_answer(
            query_plan=query_plan,
            execution_plan=plan,
            record=record,
            outputs={"calculate_ratio": 2.5},
            allow_legacy_unitless=True,
        )


def test_ad_hoc_provenance_mismatch_is_reported_as_a_grounding_mismatch() -> None:
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    bad = _adhoc_receipt(spec, binding, derived_output_id="adhoc_" + "0" * 32)
    with pytest.raises(GroundingIntegrityError, match="execution_record_verify_missing"):
        ground_execution_answer(
            query_plan=query_plan,
            execution_plan=plan,
            record=_record(plan, bad),
            outputs={"calculate_adhoc": 2.5},
            allow_legacy_unitless=True,
        )


def test_matching_ad_hoc_receipt_has_no_mismatch_flag() -> None:
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    spec = _spec()
    binding = _binding(spec)
    query_plan = _query_plan()
    context = _context()
    plan = _execution_plan(query_plan, context, spec, binding)
    good = _adhoc_receipt(spec, binding)
    with pytest.raises(GroundingIntegrityError, match="execution_record_verify_missing"):
        ground_execution_answer(
            query_plan=query_plan,
            execution_plan=plan,
            record=_record(plan, good),
            outputs={"calculate_adhoc": 2.5},
            allow_legacy_unitless=True,
        )


def test_missing_or_wrong_kind_calculation_step_is_also_a_mismatch() -> None:
    """R4 found these two were silent: the isinstance guard fired before the
    provenance comparison.  A calculation receipt whose step is absent from the
    plan, or is a different step kind, is the same re-binding mismatch class."""
    from src.nl2sql.orchestration.grounding import ground_execution_answer

    query_plan, context, plan = _trusted_plan()
    # (a) missing step: the receipt names a step_id that is not in the plan
    orphan = _trusted_receipt(step_id="ghost_calc")
    with pytest.raises(GroundingIntegrityError, match="execution_record_unknown_step"):
        ground_execution_answer(
            query_plan=query_plan,
            execution_plan=plan,
            record=_record(plan, orphan),
            outputs={"ghost_calc": 2.5},
            allow_legacy_unitless=True,
        )
    # (b) wrong step kind: a calculation receipt matched to a fetch step id
    wrong_kind = _trusted_receipt(step_id="fetch_numerator")
    with pytest.raises(GroundingIntegrityError, match="execution_record_step_kind_mismatch"):
        ground_execution_answer(
            query_plan=query_plan,
            execution_plan=plan,
            record=_record(plan, wrong_kind),
            outputs={"fetch_numerator": 2.5},
            allow_legacy_unitless=True,
        )


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
        output_digest=canonical_json_digest(5),
    )
    with pytest.raises(GroundingIntegrityError, match="execution_record_verify_missing"):
        ground_execution_answer(
            query_plan=query_plan,
            execution_plan=plan,
            record=_record(plan, dep),
            outputs={"fetch_numerator": 5},
            allow_legacy_unitless=True,
        )
    # a legitimate unavailable/no-data result is NOT a mismatch either
    good = _adhoc_receipt(
        spec, binding, output_digest=canonical_json_digest(None)
    )
    with pytest.raises(GroundingIntegrityError, match="execution_record_verify_missing"):
        ground_execution_answer(
            query_plan=query_plan,
            execution_plan=plan,
            record=_record(plan, good),
            outputs={"calculate_adhoc": None},
            allow_legacy_unitless=True,
        )


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
        allow_legacy_unitless=True,
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
        allow_legacy_unitless=True,
    ) == ()


@pytest.mark.parametrize("field", ["template_id", "template_version"])
def test_ad_hoc_receipt_rejects_template_provenance(field: str) -> None:
    spec = _spec()
    binding = _binding(spec)
    with pytest.raises(ValidationError):
        _adhoc_receipt(spec, binding, **{field: "ratio"})


def test_canonical_metric_grounding_is_unchanged() -> None:
    query_plan = _query_plan().model_copy(
        update={"metric_keys": ("metric.revenue",)}
    )
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
        output_digest=canonical_json_digest({"value": 7}),
    )
    facts = build_answer_facts(
        query_plan=query_plan,
        execution_plan=plan,
        record=_record(plan, receipt),
        outputs={"fetch_metrics": {"value": 7}},
        allow_legacy_unitless=True,
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
    good = _adhoc_receipt(
        spec, binding, output_digest=canonical_json_digest(None)
    )
    # present key with JSON null is PRESENT for the trust check
    present_null = build_answer_facts(
        query_plan=_query_plan(),
        execution_plan=plan,
        record=_record(plan, good),
        outputs={"calculate_adhoc": None},
        allow_legacy_unitless=True,
    )
    assert len(present_null) == 1
    assert present_null[0].status == "unavailable"
    # missing key produces no derived fact at all
    with pytest.raises(GroundingIntegrityError, match="grounding_output_missing"):
        build_answer_facts(
            query_plan=_query_plan(),
            execution_plan=plan,
            record=_record(plan, good),
            outputs={},
            allow_legacy_unitless=True,
        )


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
        "output_digest": canonical_json_digest(2.5),
        "template_id": "ratio",
        "template_version": "1.0",
        "binding_checksum": "c" * 64,
        "output_metric_key": "metric.revenue",
    }
    base.update(overrides)
    return PlanStepReceipt(**base)


def _complete_trusted_record(plan: ExecutionPlan) -> PlanExecutionRecord:
    receipts = (
        PlanStepReceipt(
            step_id="fetch_numerator",
            kind="fetch_metric",
            status="succeeded",
            elapsed_ms=1,
            output_digest="a" * 64,
        ),
        PlanStepReceipt(
            step_id="fetch_denominator",
            kind="fetch_metric",
            status="succeeded",
            elapsed_ms=1,
            output_digest="b" * 64,
        ),
        _trusted_receipt(),
        PlanStepReceipt(
            step_id="verify_result",
            kind="verify",
            status="succeeded",
            elapsed_ms=1,
            output_digest="d" * 64,
        ),
    )
    return PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=receipts,
        output_step_ids=tuple(receipt.step_id for receipt in receipts),
    )


def test_execution_record_is_bound_to_plan_and_terminal_verify() -> None:
    _query_plan_value, _context_value, plan = _trusted_plan()
    record = _complete_trusted_record(plan)
    assert validate_execution_record_against_plan(plan, record) is None


def test_receipt_validation_rejects_nonterminal_verify_plan() -> None:
    _query_plan_value, _context_value, plan = _trusted_plan()
    trailing = FetchMetricStep(
        step_id="fetch_trailing",
        metric_keys=("metric.extra",),
        depends_on=("verify_result",),
    )
    malformed = plan.model_copy(update={"steps": (*plan.steps, trailing)})
    record = _complete_trusted_record(malformed).model_copy(
        update={
            "step_receipts": (
                *_complete_trusted_record(malformed).step_receipts,
                PlanStepReceipt(
                    step_id="fetch_trailing",
                    kind="fetch_metric",
                    status="succeeded",
                    elapsed_ms=1,
                    output_digest="e" * 64,
                ),
            ),
            "output_step_ids": (
                *_complete_trusted_record(malformed).output_step_ids,
                "fetch_trailing",
            ),
        }
    )
    with pytest.raises(PlanStepError, match="execution_plan_verify_not_terminal"):
        validate_execution_record_against_plan(malformed, record)


def test_receipt_validation_rejects_incomplete_verify_input_coverage() -> None:
    _query_plan_value, _context_value, plan = _trusted_plan()
    verify = next(step for step in plan.steps if isinstance(step, VerifyStep))
    malformed = plan.model_copy(
        update={
            "steps": tuple(
                verify.model_copy(
                    update={
                        "depends_on": ("fetch_numerator", "fetch_denominator"),
                        "input_refs": ("fetch_numerator",),
                    }
                )
                if step is verify
                else step
                for step in plan.steps
            )
        }
    )
    with pytest.raises(PlanStepError, match="execution_plan_verify_coverage_incomplete"):
        validate_execution_record_against_plan(
            malformed, _complete_trusted_record(malformed)
        )


def test_receipt_validation_rejects_uncovered_successful_step() -> None:
    _query_plan_value, _context_value, plan = _trusted_plan()
    orphan = FetchMetricStep(
        step_id="fetch_orphan",
        metric_keys=("metric.extra",),
    )
    malformed = plan.model_copy(update={"steps": (*plan.steps[:-1], orphan, plan.steps[-1])})
    base = _complete_trusted_record(malformed)
    record = base.model_copy(
        update={
            "step_receipts": (
                *base.step_receipts,
                PlanStepReceipt(
                    step_id="fetch_orphan",
                    kind="fetch_metric",
                    status="succeeded",
                    elapsed_ms=1,
                    output_digest="e" * 64,
                ),
            ),
            "output_step_ids": (*base.output_step_ids, "fetch_orphan"),
        }
    )
    with pytest.raises(PlanStepError, match="execution_plan_verify_coverage_incomplete"):
        validate_execution_record_against_plan(malformed, record)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda plan, record: record.model_copy(
                update={"execution_plan_checksum": "0" * 64}
            ),
            "execution_record_plan_checksum_mismatch",
        ),
        (
            lambda plan, record: record.model_copy(
                update={
                    "step_receipts": (
                        *record.step_receipts,
                        PlanStepReceipt(
                            step_id="ghost_step",
                            kind="verify",
                            status="succeeded",
                            elapsed_ms=1,
                            output_digest="e" * 64,
                        ),
                    ),
                    "output_step_ids": (*record.output_step_ids, "ghost_step"),
                }
            ),
            "execution_record_unknown_step",
        ),
        (
            lambda plan, record: record.model_copy(
                update={
                    "step_receipts": (
                        record.step_receipts[0].model_copy(
                            update={"kind": "trusted_calculation"}
                        ),
                        *record.step_receipts[1:],
                    )
                }
            ),
            "execution_record_step_kind_mismatch",
        ),
        (
            lambda plan, record: record.model_copy(
                update={
                    "step_receipts": (
                        record.step_receipts[0].model_copy(update={"status": "failed"}),
                        *record.step_receipts[1:],
                    )
                }
            ),
            "execution_record_step_coverage_mismatch",
        ),
        (
            lambda plan, record: record.model_copy(
                update={
                    "step_receipts": record.step_receipts[:-1],
                    "output_step_ids": record.output_step_ids[:-1],
                }
            ),
            "execution_record_verify_missing",
        ),
        (
            lambda plan, record: record.model_copy(
                update={
                    "step_receipts": (*record.step_receipts, record.step_receipts[0]),
                    "output_step_ids": (*record.output_step_ids, record.output_step_ids[0]),
                }
            ),
            "execution_record_step_coverage_mismatch",
        ),
    ],
)
def test_execution_record_mutations_fail_closed(mutation, code: str) -> None:
    _query_plan_value, _context_value, plan = _trusted_plan()
    record = _complete_trusted_record(plan)
    mutated = mutation(plan, record)
    with pytest.raises(PlanStepError, match=code):
        validate_execution_record_against_plan(plan, mutated)


def test_failed_partial_record_is_allowed_but_unknown_receipt_is_not() -> None:
    _query_plan_value, _context_value, plan = _trusted_plan()
    partial = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="failed",
        step_receipts=(
            PlanStepReceipt(
                step_id="fetch_numerator",
                kind="fetch_metric",
                status="failed",
                elapsed_ms=1,
                error_code="query_failed",
            ),
        ),
        stop_reason="query_failed",
    )
    assert validate_execution_record_against_plan(plan, partial) is None
    unknown = partial.model_copy(
        update={
            "step_receipts": (
                partial.step_receipts[0].model_copy(update={"step_id": "ghost_step"}),
            )
        }
    )
    with pytest.raises(PlanStepError, match="execution_record_unknown_step"):
        validate_execution_record_against_plan(plan, unknown)


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
        allow_legacy_unitless=True,
    ) == ()


def test_canonical_grounding_requires_receipt_step_agreement() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    outputs = {"calculate_ratio": 2.5}
    # matching receipt grounds exactly one canonical fact
    good = build_answer_facts(
        query_plan=query_plan, execution_plan=plan,
        record=_record(plan, _trusted_receipt()), outputs=outputs,
        allow_legacy_unitless=True,
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
            allow_legacy_unitless=True,
        ) == (), field


def test_orphan_fetch_receipt_grounds_nothing() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    orphan = PlanStepReceipt(
        step_id="ghost_fetch", kind="fetch_metric", status="succeeded",
        elapsed_ms=1, output_digest=canonical_json_digest({"value": 999}),
    )
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum, status="succeeded",
        step_receipts=(orphan,), output_step_ids=("ghost_fetch",),
    )
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan, record=record,
        outputs={"ghost_fetch": {"value": 999}},
        allow_legacy_unitless=True,
    ) == ()
    # a fetch receipt naming a real non-fetch step must not fall back either
    wrong_kind = PlanStepReceipt(
        step_id="calculate_ratio", kind="fetch_metric", status="succeeded",
        elapsed_ms=1, output_digest=canonical_json_digest({"value": 999}),
    )
    record2 = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum, status="succeeded",
        step_receipts=(wrong_kind,), output_step_ids=("calculate_ratio",),
    )
    assert build_answer_facts(
        query_plan=query_plan, execution_plan=plan, record=record2,
        outputs={"calculate_ratio": {"value": 999}},
        allow_legacy_unitless=True,
    ) == ()


def test_grounding_rejects_metric_multi_row_output() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    output = {"rows": [{"value": 1}, {"value": 2}], "no_data": False}
    receipt = PlanStepReceipt(
        step_id="fetch_numerator",
        kind="fetch_metric",
        status="succeeded",
        elapsed_ms=1,
        output_digest=canonical_json_digest(output),
    )
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(receipt,),
        output_step_ids=(receipt.step_id,),
    )
    with pytest.raises(GroundingIntegrityError, match="grounding_result_cardinality_invalid"):
        build_answer_facts(
            query_plan=query_plan,
            execution_plan=plan,
            record=record,
            outputs={receipt.step_id: output},
            allow_legacy_unitless=True,
        )


def test_grounding_rejects_non_numeric_metric_value() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    output = {"value": {"forged": 1}}
    receipt = PlanStepReceipt(
        step_id="fetch_numerator",
        kind="fetch_metric",
        status="succeeded",
        elapsed_ms=1,
        output_digest=canonical_json_digest(output),
    )
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(receipt,),
        output_step_ids=(receipt.step_id,),
    )
    with pytest.raises(GroundingIntegrityError, match="grounding_result_type_invalid"):
        build_answer_facts(
            query_plan=query_plan,
            execution_plan=plan,
            record=record,
            outputs={receipt.step_id: output},
            allow_legacy_unitless=True,
        )


@pytest.mark.parametrize(
    "rows",
    [
        [{"status": "no_data"}],
        [{"value": 1}],
    ],
    ids=("placeholder-no-data-row", "business-row-marked-no-data"),
)
def test_grounding_requires_empty_rowset_for_no_data(rows: list[dict[str, object]]) -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    output = {"rows": rows, "no_data": True}
    receipt = PlanStepReceipt(
        step_id="fetch_numerator",
        kind="fetch_metric",
        status="succeeded",
        elapsed_ms=1,
        output_digest=canonical_json_digest(output),
    )
    record = PlanExecutionRecord(
        execution_plan_checksum=plan.checksum,
        status="succeeded",
        step_receipts=(receipt,),
        output_step_ids=(receipt.step_id,),
    )
    with pytest.raises(
        GroundingIntegrityError,
        match="grounding_result_cardinality_invalid",
    ):
        build_answer_facts(
            query_plan=query_plan,
            execution_plan=plan,
            record=record,
            outputs={receipt.step_id: output},
            allow_legacy_unitless=True,
        )


@pytest.mark.asyncio
async def test_typed_result_verifier_rejects_nested_or_null_values() -> None:
    step = VerifyStep(
        step_id="verify_result",
        input_refs=("fetch_metrics",),
        invariant_ids=("typed_result_present",),
        depends_on=("fetch_metrics",),
    )
    verifier = TypedResultVerifier()
    for malformed in (
        {"rows": [{"value": {"nested": 1}}], "no_data": False},
        {"value": None},
        {"rows": [], "no_data": False},
    ):
        with pytest.raises(PlanStepError, match="typed_result_shape_invalid"):
            await verifier.verify(step=step, inputs=(malformed,))


def test_ground_execution_answer_rejects_query_plan_hash_mismatch() -> None:
    query_plan, _context_bundle, plan = _trusted_plan()
    mismatched = query_plan.model_copy(update={"domain": "other"})
    with pytest.raises(GroundingIntegrityError, match="grounding_query_plan_mismatch"):
        ground_execution_answer(
            query_plan=mismatched,
            execution_plan=plan,
            record=PlanExecutionRecord(
                execution_plan_checksum=plan.checksum,
                status="failed",
                step_receipts=(),
                output_step_ids=(),
                stop_reason="fixture",
            ),
            outputs={},
            allow_legacy_unitless=True,
        )


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
        allow_legacy_unitless=True,
    )
    assert len(facts) == 1
    assert facts[0].derived_output_id == derived_output_id(spec, binding)
    assert facts[0].unit == spec.unit
    grounded = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=result.record,
        outputs=result.outputs,
        allow_legacy_unitless=True,
    )
    assert grounded.facts[0].unit == spec.unit

    promoted_fact = grounded.facts[0].model_copy(update={"unit": "count"})
    promoted_fact = promoted_fact.model_copy(
        update={"fact_id": _grounded_fact_id(promoted_fact)}
    )
    promoted_record = result.record.model_copy(
        update={
            "step_receipts": tuple(
                receipt.model_copy(update={"unit": "count"})
                if receipt.step_id == "calculate_adhoc"
                else receipt
                for receipt in result.record.step_receipts
            )
        }
    )
    promoted_artifact = AnswerArtifact(facts=(promoted_fact,))
    promoted_text = render_grounded_answer(
        query_plan=query_plan,
        facts=(promoted_fact,),
    )
    unit_authority_identities = {
        "calculate_adhoc": canonical_json_digest(
            {
                "kind": "ad_hoc_calculation",
                "step_id": "calculate_adhoc",
                "calculation_spec_checksum": spec.checksum,
                "execution_binding_checksum": binding.checksum,
                "unit": spec.unit,
            }
        )
    }
    seal_key = "ad-hoc-test-checkpoint-evidence-seal-key"
    promoted_record = seal_grounded_execution_checkpoint(
        key=seal_key,
        query_plan=query_plan,
        execution_plan=plan,
        execution_record=promoted_record,
        answer_artifact=promoted_artifact,
        answer_text=promoted_text,
        unit_authority_identities=unit_authority_identities,
    )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_fact_execution_mismatch"
    ):
        validate_grounded_answer_checkpoint(
            query_plan,
            promoted_artifact,
            promoted_text,
            execution_plan=plan,
            execution_record=promoted_record,
            evidence_seal_key=seal_key,
            unit_authority_identities=unit_authority_identities,
        )


@pytest.mark.asyncio
async def test_trusted_calculation_unit_rebinds_to_catalog_on_checkpoint_restore() -> None:
    template = trusted_template_registry.metadata("ratio")
    output_metric_key = "metric.revenue_ratio"
    catalog_binding = ApprovedCalculationBinding(
        canonical_metric_key=output_metric_key,
        template_id=template.template_id,
        template_version=template.version,
        template_checksum=template.checksum,
        inputs=(
            ApprovedCalculationInput(
                role="numerator",
                metric_key="metric.revenue",
                metric_contract_sha256="a" * 64,
            ),
            ApprovedCalculationInput(
                role="denominator",
                metric_key="metric.stores",
                metric_contract_sha256="b" * 64,
            ),
        ),
        binding_revision="unit-authority-test",
        semantic_release_id=str(RELEASE_ID),
        semantic_release_checksum="c" * 64,
        unit="ratio",
    )
    catalog = ApprovedCalculationCatalog((catalog_binding,))
    context = _context(
        asset_ids=("metric.revenue", "metric.stores", output_metric_key)
    )
    query_plan = _query_plan().model_copy(update={"metric_keys": (output_metric_key,)})
    identity = RequestIdentity(
        request_id=UUID("44444444-4444-4444-4444-444444444444"),
        user_id="analyst",
        permissions=frozenset({"metrics:read", "nl2sql:invoke"}),
    )
    validator = PlanValidator(calculation_catalog=catalog)
    query_validation = validator.validate_query_plan(
        plan=query_plan,
        context=context,
        identity=identity,
    )
    assert query_validation.outcome == "allow", query_validation.issues
    plan = PlanCompiler(calculation_catalog=catalog).compile(
        plan=query_plan,
        context=context,
        validation=query_validation,
    )
    execution_validation = validator.validate_execution_plan(
        execution_plan=plan,
        query_plan=query_plan,
        context=context,
        route_budget=RouteBudgetLedger(route="standard").limits,
    )
    assert execution_validation.outcome == "allow", execution_validation.issues
    result = await PlanExecutor(
        metric_runner=_MetricRunner(
            {"metric.revenue": 5, "metric.stores": 2}
        ),
        trusted_calculation_runner=RegistryTrustedCalculationRunner(),
        calculation_catalog=catalog,
    ).execute(
        query_plan=query_plan,
        context=context,
        execution_plan=plan,
        validation=execution_validation,
        expected_policy_version=validator.policy_version,
        expected_policy_checksum=validator.policy_checksum,
        budget=RouteBudgetLedger(route="standard"),
        deadline_ms=4_000,
    )
    assert result.record.status == "succeeded", result.record.stop_reason
    calculation_receipt = next(
        receipt
        for receipt in result.record.step_receipts
        if receipt.kind == "trusted_calculation"
    )
    assert calculation_receipt.unit == catalog_binding.unit
    changed_unit_record = result.record.model_copy(
        update={
            "step_receipts": tuple(
                receipt.model_copy(update={"unit": "count"})
                if receipt.step_id == calculation_receipt.step_id
                else receipt
                for receipt in result.record.step_receipts
            )
        }
    )
    with pytest.raises(
        GroundingIntegrityError, match="grounding_unit_authority_mismatch"
    ):
        ground_execution_answer(
            query_plan=query_plan,
            execution_plan=plan,
            record=changed_unit_record,
            outputs=result.outputs,
            authoritative_units_by_step={
                calculation_receipt.step_id: catalog_binding.unit
            },
        )
    grounded = ground_execution_answer(
        query_plan=query_plan,
        execution_plan=plan,
        record=result.record,
        outputs=result.outputs,
        authoritative_units_by_step={"calculate_metric": catalog_binding.unit},
    )
    assert grounded.facts[0].unit == catalog_binding.unit

    promoted_fact = grounded.facts[0].model_copy(update={"unit": "count"})
    promoted_fact = promoted_fact.model_copy(
        update={"fact_id": _grounded_fact_id(promoted_fact)}
    )
    promoted_record = result.record.model_copy(
        update={
            "step_receipts": tuple(
                receipt.model_copy(update={"unit": "count"})
                if receipt.step_id == calculation_receipt.step_id
                else receipt
                for receipt in result.record.step_receipts
            )
        }
    )
    promoted_artifact = AnswerArtifact(facts=(promoted_fact,))
    promoted_text = render_grounded_answer(
        query_plan=query_plan,
        facts=(promoted_fact,),
    )
    unit_authority_identities = {
        calculation_receipt.step_id: canonical_json_digest(
            {
                "kind": "trusted_calculation",
                "step_id": calculation_receipt.step_id,
                "catalog_checksum": catalog.checksum,
                "binding_checksum": catalog_binding.checksum,
                "unit": catalog_binding.unit,
            }
        )
    }
    seal_key = "trusted-test-checkpoint-evidence-seal-key"
    promoted_record = seal_grounded_execution_checkpoint(
        key=seal_key,
        query_plan=query_plan,
        execution_plan=plan,
        execution_record=promoted_record,
        answer_artifact=promoted_artifact,
        answer_text=promoted_text,
        unit_authority_identities=unit_authority_identities,
    )
    with pytest.raises(
        AnalysisEvidenceError, match="analysis_fact_unit_authority_missing"
    ):
        validate_grounded_answer_checkpoint(
            query_plan,
            promoted_artifact,
            promoted_text,
            execution_plan=plan,
            execution_record=promoted_record,
            authoritative_units={output_metric_key: catalog_binding.unit},
            evidence_seal_key=seal_key,
            unit_authority_identities=unit_authority_identities,
        )


@pytest.mark.asyncio
async def test_engine_checkpoint_restore_reuses_trusted_calculation_catalog_authority() -> None:
    catalog_binding = _approved_ratio_binding()
    catalog = ApprovedCalculationCatalog((catalog_binding,))
    checkpointer = MemorySaver()
    gateway = QueryGateway(None, schema="ai_views")  # type: ignore[arg-type]
    gateway.execute = AsyncMock(
        return_value=QueryReceipt(
            accepted=True,
            sql="SELECT synthetic",
            sql_fingerprint=SQL_FINGERPRINT,
            row_count=1,
            policy_outcome="allow",
            max_rows=200,
        )
    )  # type: ignore[method-assign]
    metric_runner = _GatewayMetricRunner(
        {"metric.revenue": 5, "metric.stores": 2}, gateway
    )
    calculation_runner = _CountingTrustedRunner()
    provider = _EngineAnalysisProvider()
    engine = _trusted_engine(
        catalog=catalog,
        checkpointer=checkpointer,
        metric_runner=metric_runner,
        calculation_runner=calculation_runner,
        provider=provider,
    )
    config = _trusted_request_config()
    initial = await engine.ainvoke(_trusted_graph_input(), config)

    assert initial.get("stop_reason") is None, initial.get("stop_reason")
    assert provider.calls == 1
    assert metric_runner.execute_calls == 2
    assert gateway.execute.await_count == 2
    assert calculation_runner.calls == 1
    assert initial["grounded_answer_artifact"]["facts"][0]["unit"] == "ratio"
    assert any(
        event["name"] == "governed_analysis_completed"
        for event in initial["trace_events"]
    )

    history = [snapshot async for snapshot in engine.aget_state_history(config)]
    execution_checkpoint = next(
        snapshot for snapshot in history if "analysis" in snapshot.next
    )
    execution_plan = ExecutionPlan.model_validate(
        execution_checkpoint.values["execution_plan"]
    )
    assert tuple(step.kind for step in execution_plan.steps) == (
        "fetch_metric",
        "fetch_metric",
        "trusted_calculation",
        "verify",
    )
    assert execution_checkpoint.values["execution_record"]["evidence_seal"]["version"] == "v1"
    resume_config = dict(config)
    resume_config["configurable"] = {
        **config["configurable"],
        **execution_checkpoint.config["configurable"],
    }
    restored = await engine.ainvoke(None, resume_config)

    assert restored.get("stop_reason") is None
    assert provider.calls == 2
    assert metric_runner.execute_calls == 2
    assert gateway.execute.await_count == 2
    assert calculation_runner.calls == 1
    assert restored["grounded_answer_artifact"]["facts"][0]["unit"] == "ratio"
    assert any(
        block.get("type") == "provenance"
        for block in restored.get("response_blocks", [])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_stop_reason"),
    [
        ("catalog_checksum", "checkpoint_evidence_seal_invalid"),
        ("binding_checksum", "analysis_calculation_unit_authority_missing"),
        ("missing_catalog", "analysis_calculation_unit_authority_missing"),
        (
            "receipt_and_fact_unit",
            "checkpoint_calculation_unit_authority_missing",
        ),
    ],
)
async def test_engine_checkpoint_restore_rejects_trusted_calculation_authority_mutations(
    mutation: str,
    expected_stop_reason: str,
) -> None:
    catalog_binding = _approved_ratio_binding()
    catalog = ApprovedCalculationCatalog((catalog_binding,))
    checkpointer = MemorySaver()
    gateway = QueryGateway(None, schema="ai_views")  # type: ignore[arg-type]
    gateway.execute = AsyncMock(
        return_value=QueryReceipt(
            accepted=True,
            sql="SELECT synthetic",
            sql_fingerprint=SQL_FINGERPRINT,
            row_count=1,
            policy_outcome="allow",
            max_rows=200,
        )
    )  # type: ignore[method-assign]
    metric_runner = _GatewayMetricRunner(
        {"metric.revenue": 5, "metric.stores": 2}, gateway
    )
    calculation_runner = _CountingTrustedRunner()
    provider = _EngineAnalysisProvider()
    engine = _trusted_engine(
        catalog=catalog,
        checkpointer=checkpointer,
        metric_runner=metric_runner,
        calculation_runner=calculation_runner,
        provider=provider,
    )
    config = _trusted_request_config()
    initial = await engine.ainvoke(_trusted_graph_input(), config)

    assert initial.get("stop_reason") is None, initial.get("stop_reason")
    assert provider.calls == 1
    assert metric_runner.execute_calls == 2
    assert gateway.execute.await_count == 2
    assert calculation_runner.calls == 1

    history = [snapshot async for snapshot in engine.aget_state_history(config)]
    execution_checkpoint = next(
        snapshot for snapshot in history if "analysis" in snapshot.next
    )
    restore_catalog: ApprovedCalculationCatalog | None
    if mutation == "catalog_checksum":
        unrelated_binding = _approved_ratio_binding(
            "metric.other_ratio", binding_revision="unrelated-binding"
        )
        restore_catalog = ApprovedCalculationCatalog(
            (catalog_binding, unrelated_binding)
        )
    elif mutation == "binding_checksum":
        changed_binding = _approved_ratio_binding(
            binding_revision="changed-binding-revision"
        )
        restore_catalog = ApprovedCalculationCatalog((changed_binding,))
    elif mutation == "missing_catalog":
        restore_catalog = None
    else:
        restore_catalog = catalog

    if mutation == "receipt_and_fact_unit":
        saved = (await engine.aget_state(config)).values
        record = PlanExecutionRecord.model_validate(saved["execution_record"])
        artifact = AnswerArtifact.model_validate(saved["grounded_answer_artifact"])
        fact = artifact.facts[0].model_copy(update={"unit": "count"})
        fact = fact.model_copy(update={"fact_id": _grounded_fact_id(fact)})
        artifact = artifact.model_copy(update={"facts": (fact,)})
        record = record.model_copy(
            update={
                "step_receipts": tuple(
                    receipt.model_copy(update={"unit": "count"})
                    if receipt.kind == "trusted_calculation"
                    else receipt
                    for receipt in record.step_receipts
                )
            }
        )
        answer_text = render_grounded_answer(
            query_plan=QueryPlan.model_validate(saved["query_plan"]), facts=(fact,)
        )
        await engine.aupdate_state(
            config,
            {
                "execution_record": record.model_dump(mode="json"),
                "grounded_answer_artifact": artifact.model_dump(mode="json"),
                "grounded_answer_text": answer_text,
            },
            as_node="execute",
        )
        restore_engine = engine
        resume_config = config
    else:
        restore_engine = _trusted_engine(
            catalog=restore_catalog,
            checkpointer=checkpointer,
            metric_runner=metric_runner,
            calculation_runner=calculation_runner,
            provider=provider,
        )
        resume_config = dict(config)
        resume_config["configurable"] = {
            **config["configurable"],
            **execution_checkpoint.config["configurable"],
        }

    restored = await restore_engine.ainvoke(None, resume_config)

    assert restored.get("stop_reason") == expected_stop_reason
    assert restored.get("response_blocks", []) == []
    assert provider.calls == 1
    assert metric_runner.execute_calls == 2
    assert gateway.execute.await_count == 2
    assert calculation_runner.calls == 1
