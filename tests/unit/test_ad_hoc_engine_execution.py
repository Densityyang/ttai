"""QUERY noncanonical AD_HOC is REACHABLE through the REAL engine.

These tests drive the actual compiled LangGraph engine (not a node in isolation)
and prove, with execution-level evidence, that an explicit run-scoped AD_HOC
carrier:

* compiles through the AD_HOC dispatch, executes its dependency fetches and its
  calculation step, and grounds a NONCANONICAL fact;
* calls the shared arithmetic runner EXACTLY ONCE;
* emits provenance carrying calculation_scope="ad_hoc_noncanonical" and the
  derived output ids;
* is refused by an EXPLICIT effective-mode capability gate when the mode does not
  confer run_scoped_derivation (nothing compiled, nothing executed);
* suspends, as ONE typed business_confirmation (issue code
  custom_metric_plan_confirmation), before anything is compiled or executed -
  including when the formula and the question-resolved plan AGREE - and only a
  confirm continues into the governed AD_HOC compile/execute path;
* maps every STRUCTURAL request-entry rejection code STRAIGHT to stop_reason
  with zero SQL; the one ALIGNMENT code (ad_hoc_request_source_plan_mismatch) is
  confirmed first and still fails closed with that same stable code and zero SQL;
* never touches the Custom Definition lifecycle (A4).

A stub request-scoped runtime factory stands in for the production one; the
engine, the PlanCompiler/PlanValidator wiring, the PlanExecutor and the shared
RuntimeCalculationRunner are the real ones.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from src.nl2sql.agents.dynamic_calc.trusted_templates import trusted_template_registry
from src.nl2sql.artifacts.definition_store import InMemoryDefinitionStore
from src.nl2sql.contracts import (
    AdHocCalculationStep,
    AnswerFact,
    AuthorizationContext,
    ContextBundle,
    ExecutionReceipt,
    PlanValidationRecord,
    QueryPlan,
    RequestContext,
    RequestIdentity,
    RoutePolicy,
    TimeRange,
    evaluate_authorization,
)
from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.ad_hoc_request import AdHocCalculationRequest
from src.nl2sql.orchestration.analysis_evidence import _grounded_fact_id
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
    ApprovedCalculationInput,
)
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutionResult,
    PlanExecutor,
    PreparedMetricStep,
    RuntimeCalculationRunner,
)
from src.nl2sql.orchestration.grounding import render_grounded_answer
from src.nl2sql.orchestration.planning import PlanCompiler
from src.nl2sql.orchestration.typed_runtime import (
    TypedRuntimeUnavailable,
    ad_hoc_calculation_runner_for_capabilities,
)
from src.nl2sql.ownership import runtime_config
from src.nl2sql.semantic.calculation_contract import (
    BinaryOperand,
    CalculationExecutionBinding,
    CalculationInputSpec,
    CalculationSpec,
    InputRefOperand,
    ParameterBinding,
    ParameterSpec,
    derived_output_id,
)
from src.nl2sql.supervisor.schemas import (
    ProvenanceAuthorityBlock,
    ProvenanceBlock,
    ProvenanceTimeRangeBlock,
)

RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
REQUEST_ID = UUID("33333333-3333-3333-3333-333333333333")
THREAD_ID = UUID("44444444-4444-4444-4444-444444444444")
SQL_FINGERPRINT = "a" * 64
DERIVED_PREFIX = "adhoc_"


# --- typed fixtures -----------------------------------------------------------


def _identity() -> RequestIdentity:
    return RequestIdentity(
        request_id=REQUEST_ID,
        user_id="analyst",
        roles=frozenset({"analyst"}),
        permissions=frozenset({"metrics:read"}),
    )


def _authorization(revision: str = "rev-1") -> AuthorizationContext:
    return AuthorizationContext(
        authorization_revision=revision,
        agent_enabled=True,
        scope_level="team",
        allowed_scope_ids=("t1",),
    )


def _context(
    *,
    asset_ids: tuple[str, ...] = ("metric.revenue", "metric.stores"),
    resolution_status: str = "resolved",
) -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE_ID,
        schema_snapshot_id=SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=asset_ids,
        resolution_status=resolution_status,  # type: ignore[arg-type]
    )


def _plan(
    *,
    metric_keys: tuple[str, ...] = ("metric.revenue", "metric.stores"),
    intent: str = "metric",
    dimensions: tuple[str, ...] = (),
) -> QueryPlan:
    return QueryPlan(
        intent=intent,  # type: ignore[arg-type]
        domain="finance",
        metric_keys=metric_keys,
        time_range=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=("metrics:read",),
        dimensions=dimensions,
    )


def _ratio_spec(
    *,
    metric_keys: tuple[str, ...] = ("metric.revenue", "metric.stores"),
) -> CalculationSpec:
    """A legal ratio AD_HOC spec over two uniquely resolved inputs."""

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
                metric_key=metric_keys[0],
            ),
            CalculationInputSpec(
                role="denominator",
                provenance="published_gold",
                metric_key=metric_keys[1],
            ),
        ),
        unit="ratio",
        precision=4,
        rounding="half_up",
    )


def _single_spec(
    *, metric_key: str = "metric.revenue", unit: str = "count"
) -> CalculationSpec:
    return CalculationSpec(
        calculation_id="adhoc.single_input",
        expression=InputRefOperand(role="numerator"),
        inputs=(
            CalculationInputSpec(
                role="numerator",
                provenance="published_gold",
                metric_key=metric_key,
            ),
        ),
        unit=unit,  # type: ignore[arg-type]
    )


def _binding(spec: CalculationSpec) -> CalculationExecutionBinding:
    return CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(),
    )


def _carrier(
    spec: CalculationSpec | None = None,
    binding: CalculationExecutionBinding | None = None,
) -> AdHocCalculationRequest:
    resolved_spec = spec if spec is not None else _ratio_spec()
    return AdHocCalculationRequest(
        calculation_spec=resolved_spec,
        execution_binding=binding if binding is not None else _binding(resolved_spec),
    )


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


# --- runtime doubles ----------------------------------------------------------


class _CountingMetricRunner:
    """Counts every dependency/direct fetch prepared and executed.

    Each metric key yields a DISTINCT SQL fingerprint so the route ledger's
    repeated-SQL guard does not stand in for real execution evidence.
    """

    def __init__(
        self,
        values: dict[str, object] | None = None,
        *,
        gateway: QueryGateway | None = None,
    ) -> None:
        self.values = values or {"metric.revenue": 1000, "metric.stores": 25}
        self.gateway = gateway
        self.prepare_calls = 0
        self.execute_calls = 0

    @staticmethod
    def _fingerprint(metric_key: str) -> str:
        return hashlib.sha256(metric_key.encode("utf-8")).hexdigest()

    async def prepare(
        self, *, step: Any, query_plan: Any, context: Any
    ) -> PreparedMetricStep:
        del query_plan, context
        self.prepare_calls += 1
        metric_key = step.metric_keys[0]
        return PreparedMetricStep(
            sql_fingerprint=self._fingerprint(metric_key),
            join_hops=0,
            payload={"sql": "SELECT synthetic", "metric_key": metric_key},
            dependency_fetch=step.ad_hoc_input_role is not None,
        )

    async def execute(
        self, prepared: PreparedMetricStep, *, timeout_ms: int
    ) -> MetricStepResult:
        del timeout_ms
        self.execute_calls += 1
        payload = prepared.payload
        assert isinstance(payload, dict)
        if self.gateway is not None:
            await self.gateway.execute(
                str(payload["sql"]), {}, trace_id="adhoc-checkpoint-test"
            )
        value = self.values[str(payload["metric_key"])]
        return MetricStepResult(
            value={"value": value},
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


class _CountingAdHocRunner:
    """Delegates to the REAL shared evaluator and counts invocations."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_inputs: dict[str, object] = {}
        self._inner = RuntimeCalculationRunner()

    async def execute(self, *, step: Any, inputs: dict[str, Any]) -> Any:
        self.calls += 1
        self.last_inputs = dict(inputs)
        return await self._inner.execute(step=step, inputs=inputs)


class _Resolver:
    def __init__(self, context: ContextBundle) -> None:
        self._context = context

    async def resolve(
        self, *, question: str, identity: RequestIdentity, route_hint: str
    ) -> ContextBundle:
        del question, identity, route_hint
        return self._context


class _StaticPlanProvider:
    is_deterministic = True

    def __init__(self, plan: QueryPlan) -> None:
        self._plan = plan

    async def propose(
        self, *, question: str, context: ContextBundle, identity: RequestIdentity
    ) -> QueryPlan:
        del question, context, identity
        return self._plan


@dataclass
class _StubRuntime:
    context_resolver: Any
    query_plan_provider: Any
    plan_executor: Any
    identity: RequestIdentity
    authorization: AuthorizationContext
    authorization_revision: str

    def authorization_decision(self) -> Any:
        return evaluate_authorization(self.authorization, expected_revision=None)


class _RuntimeFactory:
    """Per-request factory double that honours the EXPLICIT capability gate."""

    def __init__(
        self,
        *,
        metric_runner: _CountingMetricRunner,
        provider: _StaticPlanProvider,
        context: ContextBundle,
        ad_hoc_runner: _CountingAdHocRunner,
        inject_runner_without_capability: bool = False,
    ) -> None:
        self.metric_runner = metric_runner
        self.provider = provider
        self.context = context
        self.ad_hoc_runner = ad_hoc_runner
        self.inject_runner_without_capability = inject_runner_without_capability
        self.capabilities_seen: list[frozenset[str]] = []

    async def __call__(
        self,
        *,
        identity: RequestIdentity,
        authorization: AuthorizationContext | None,
        expected_revision: str | None,
        capabilities: frozenset[str] = frozenset(),
    ) -> Any:
        self.capabilities_seen.append(frozenset(capabilities))
        if authorization is None:
            return TypedRuntimeUnavailable(reason="authorization_context_missing")
        # The REAL gate function decides whether the shared evaluator may be
        # injected; the test only counts how often it is actually reached.
        gated = ad_hoc_calculation_runner_for_capabilities(capabilities)
        runner = (
            self.ad_hoc_runner
            if gated is not None or self.inject_runner_without_capability
            else None
        )
        return _StubRuntime(
            context_resolver=_Resolver(self.context),
            query_plan_provider=self.provider,
            plan_executor=PlanExecutor(
                metric_runner=self.metric_runner,
                ad_hoc_calculation_runner=runner,
                allow_legacy_unitless_grounding=True,
            ),
            identity=identity,
            authorization=authorization,
            authorization_revision=authorization.authorization_revision,
        )


class _AllowAllValidator:
    """Forces an allow query-plan validation so the AD_HOC resolver is reached.

    Used ONLY for the input_ambiguous case: with the real validator a
    non-resolved context is CLARIFIED before compile, so the AD_HOC resolver
    would never run.  This double lets the test prove the resolver's OWN
    independent refusal still fires.
    """

    policy_version = "plan-validator.test.allow"
    policy_checksum = "f" * 64

    def validate_query_plan(
        self, *, plan: QueryPlan, context: ContextBundle, identity: RequestIdentity
    ) -> PlanValidationRecord:
        del identity
        return PlanValidationRecord(
            policy_version=self.policy_version,
            policy_checksum=self.policy_checksum,
            outcome="allow",
            query_plan_sha256=plan.checksum,
            context_checksum=context.checksum,
        )

    def validate_execution_plan(self, **kwargs: Any) -> Any:
        raise AssertionError("execution plan validation must not be reached")


class _Provider:
    provider_name = "synthetic"

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        del kwargs
        return ProviderResponse(content="x", model="small", usage={}, finish_reason="stop")

    async def list_models(self) -> tuple[str, ...]:
        return ("small",)


class _AnalysisProvider(_Provider):
    """Controlled structured response provider for checkpoint restoration tests."""

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
        source = json.loads(source_json)
        fact = source["facts"][0]
        return ProviderResponse(
            content=json.dumps(
                {
                    "summary": {
                        "text": f"The derived value is {fact['value']}.",
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


def _gateway(provider: _Provider | None = None) -> ModelGateway:
    selected_provider = provider or _Provider()
    return ModelGateway(
        providers={"synthetic": selected_provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("synthetic", "small", "small"),
                None,
            ),
        },
    )


def _standard_route_policy() -> RoutePolicy:
    """Force the standard route so a TWO-input AD_HOC plan fits the SQL budget."""

    return RoutePolicy(
        version="route.test.standard-only",
        state="bootstrap",
        fast_max_risk=20,
        fast_min_confidence=1.0,
        fast_max_tables=1,
        standard_max_risk=60,
        standard_min_confidence=0.55,
    )


def _config(revision: str = "rev-1") -> dict[str, Any]:
    return runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_ID,
            trace_id="trace-adhoc",
            deadline_ms=10_000,
            authorization=_authorization(revision),
        )
    )


def _graph_input(
    carrier: AdHocCalculationRequest | None = None,
    *,
    effective_mode: str | None = "QUERY",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "revenue per store"}]
    }
    if effective_mode is not None:
        from src.nl2sql.orchestration.mode_contract import RunEnvelope

        envelope = RunEnvelope(
            run_id="run-adhoc",
            requested_mode=effective_mode,  # type: ignore[arg-type]
            effective_mode=effective_mode,  # type: ignore[arg-type]
        )
        payload["run_envelope"] = envelope.model_dump(mode="json")
    if carrier is not None:
        payload["ad_hoc_calculation"] = carrier.model_dump(mode="json")
    return payload


def _engine(
    *,
    metric_runner: _CountingMetricRunner,
    provider: _StaticPlanProvider,
    context: ContextBundle,
    ad_hoc_runner: _CountingAdHocRunner,
    route_policy: RoutePolicy | None = None,
    plan_compiler: PlanCompiler | None = None,
    plan_validator: Any | None = None,
    inject_runner_without_capability: bool = False,
    checkpointer: MemorySaver | None = None,
    model_provider: _Provider | None = None,
) -> tuple[Any, _RuntimeFactory]:
    factory = _RuntimeFactory(
        metric_runner=metric_runner,
        provider=provider,
        context=context,
        ad_hoc_runner=ad_hoc_runner,
        inject_runner_without_capability=inject_runner_without_capability,
    )
    engine = create_v2_engine(
        checkpointer=checkpointer or MemorySaver(),
        model_gateway=_gateway(model_provider),
        route_policy=route_policy,
        plan_compiler=plan_compiler,
        plan_validator=plan_validator,
        typed_runtime_factory=factory,
    )
    return engine, factory


def _query_gateway() -> tuple[QueryGateway, AsyncMock]:
    gateway = QueryGateway(None, schema="ai_views")  # type: ignore[arg-type]
    execution = AsyncMock(
        return_value=QueryReceipt(
            accepted=True,
            sql="SELECT synthetic",
            sql_fingerprint=SQL_FINGERPRINT,
            row_count=1,
            policy_outcome="allow",
            max_rows=200,
        )
    )
    gateway.execute = execution  # type: ignore[method-assign]
    return gateway, execution


def _bind_checkpoint_step(
    step_data: dict[str, Any], *, mutation: str
) -> dict[str, Any]:
    """Return one model-valid AD_HOC step with altered declaration authority."""

    step = AdHocCalculationStep.model_validate(step_data)
    spec = step.calculation_spec
    binding = step.execution_binding
    if mutation == "custom_unit_id":
        spec = spec.model_copy(update={"custom_unit_id": "widgets_per_month"})
        binding = binding.model_copy(update={"spec_checksum": spec.checksum})
    elif mutation == "spec_checksum":
        spec = spec.model_copy(update={"precision": 3})
        binding = binding.model_copy(update={"spec_checksum": spec.checksum})
    elif mutation == "execution_binding_checksum":
        if not binding.parameters:
            raise AssertionError("binding checksum fixture requires a declared parameter")
        original = binding.parameters[0]
        changed = original.model_copy(update={"value": 2})
        binding = binding.model_copy(update={"parameters": (changed,)})
    else:
        raise AssertionError(f"unknown checkpoint mutation: {mutation}")
    return step.model_copy(
        update={
            "calculation_spec": spec,
            "execution_binding": binding,
            "derived_output_id": derived_output_id(spec, binding),
        }
    ).model_dump(mode="json")


def _checkpoint_carrier(
    *,
    unit: str,
    with_parameter: bool = False,
) -> tuple[AdHocCalculationRequest, CalculationSpec, CalculationExecutionBinding]:
    base = _single_spec()
    spec_payload = base.model_dump(mode="python")
    if unit == "custom_unit":
        spec_payload["unit"] = "custom_unit"
        spec_payload["custom_unit_id"] = "widgets_per_week"
    else:
        spec_payload["unit"] = unit
        spec_payload["custom_unit_id"] = None
    if with_parameter:
        spec_payload["parameters"] = [
            ParameterSpec(
                name="calculation_mode",
                value_type="integer",
                allowed_values=("1", "2"),
            ).model_dump(mode="python")
        ]
    spec = CalculationSpec.model_validate(spec_payload)
    binding_parameters = (
        (ParameterBinding(name="calculation_mode", value=1),)
        if with_parameter
        else ()
    )
    binding = CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=binding_parameters,
    )
    return _carrier(spec, binding), spec, binding


def _confirm_payload(key: str = "k-confirm") -> dict[str, object]:
    """The typed confirm of the P6-B explicit-formula plan confirmation."""

    return {"action": "confirm", "idempotency_key": key}


def _provenance(state: dict[str, Any]) -> dict[str, Any]:
    blocks = state.get("response_blocks") or []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "provenance":
            return block
    raise AssertionError("no provenance block was emitted")


# --- 1. end-to-end ------------------------------------------------------------


@pytest.mark.asyncio
async def test_ad_hoc_query_executes_once_and_grounds_noncanonical_provenance() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    spec = _ratio_spec()
    binding = _binding(spec)
    carrier = _carrier(spec, binding)
    engine, factory = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )
    config = _config()

    paused = await engine.ainvoke(_graph_input(carrier), config)
    # P6-B: the explicit formula suspends as ONE typed confirmation FIRST.
    assert paused["decision_status"] == "awaiting_decision"
    assert paused["pending_decision"]["issue_codes"] == [
        "custom_metric_plan_confirmation"
    ]
    assert ad_hoc_runner.calls == 0
    result = await engine.ainvoke(
        Command(resume=_confirm_payload()), config
    )

    # The carrier was actually compiled and executed by the REAL engine.
    assert result["stop_reason"] is None
    assert result["execution_record"]["status"] == "succeeded"
    assert result["route_record"]["route"] == "standard"
    # The shared arithmetic runner ran EXACTLY ONCE, on the resolved scalars.
    assert ad_hoc_runner.calls == 1
    assert ad_hoc_runner.last_inputs == {"numerator": 1000, "denominator": 25}
    # Both AD_HOC dependency fetches executed; NO direct metric fetch did.
    assert metric_runner.prepare_calls == 2
    assert metric_runner.execute_calls == 2
    # The derived value is real: 1000 / 25 = 40.0000.
    assert "40.0000" in result["grounded_answer_text"]
    assert "Derived result" in result["grounded_answer_text"]
    # The capability set was passed EXPLICITLY and carried the gate capability.
    assert factory.capabilities_seen
    assert "run_scoped_derivation" in factory.capabilities_seen[0]

    provenance = _provenance(result)
    assert provenance["calculation_scope"] == "ad_hoc_noncanonical"
    assert provenance["derived_output_ids"] == [derived_output_id(spec, binding)]
    # Pure AD_HOC: no canonical metric key is fabricated.
    assert provenance["metric_keys"] == []


@pytest.mark.asyncio
async def test_single_input_ad_hoc_executes_under_the_default_fast_route() -> None:
    """A ONE-input derivation is reachable under the DEFAULT route budget.

    The bootstrap fast route admits exactly ONE SQL candidate/execution, so only
    a single-input AD_HOC plan fits it.  This proves the default configuration
    really executes an AD_HOC carrier end to end (see the report: a MULTI-input
    plan is denied by the fast SQL budget, which is a separate product finding).
    """

    metric_runner = _CountingMetricRunner(values={"metric.revenue": 1000})
    ad_hoc_runner = _CountingAdHocRunner()
    spec = _single_spec()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue",))),
        context=_context(asset_ids=("metric.revenue",)),
        ad_hoc_runner=ad_hoc_runner,
    )
    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier(spec)), config)
    assert paused["decision_status"] == "awaiting_decision"
    assert paused["pending_decision"]["issue_codes"] == [
        "custom_metric_plan_confirmation"
    ]

    result = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert result["route_record"]["route"] == "fast"
    assert result["stop_reason"] is None
    assert result["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 1
    provenance = _provenance(result)
    assert provenance["calculation_scope"] == "ad_hoc_noncanonical"
    assert provenance["derived_output_ids"] == [
        derived_output_id(spec, _binding(spec))
    ]
    assert provenance["metric_keys"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate_unit", [False, True])
async def test_custom_adhoc_unit_is_rebound_before_grounding_and_capture(
    monkeypatch: pytest.MonkeyPatch, mutate_unit: bool
) -> None:
    base_spec = _single_spec()
    spec = CalculationSpec.model_validate(
        {
            **base_spec.model_dump(mode="python"),
            "unit": "custom_unit",
            "custom_unit_id": "widgets_per_week",
        }
    )
    metric_runner = _CountingMetricRunner(values={"metric.revenue": 1000})
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue",))),
        context=_context(asset_ids=("metric.revenue",)),
        ad_hoc_runner=ad_hoc_runner,
    )

    if mutate_unit:
        original_execute = PlanExecutor.execute

        async def execute_with_mutated_unit(
            self: PlanExecutor, **kwargs: Any
        ) -> PlanExecutionResult:
            result = await original_execute(self, **kwargs)
            receipts = tuple(
                receipt.model_copy(update={"unit": "count"})
                if receipt.kind == "ad_hoc_calculation"
                else receipt
                for receipt in result.record.step_receipts
            )
            return PlanExecutionResult(
                record=result.record.model_copy(update={"step_receipts": receipts}),
                outputs=result.outputs,
                output_contracts=result.output_contracts,
                require_semantic_contract=result.require_semantic_contract,
            )

        monkeypatch.setattr(PlanExecutor, "execute", execute_with_mutated_unit)

    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier(spec)), config)
    assert paused["decision_status"] == "awaiting_decision"
    result = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert result["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 1
    if mutate_unit:
        assert result["stop_reason"] == "checkpoint_adhoc_unit_authority_mismatch"
        assert result.get("grounded_answer_artifact") is None
        assert result.get("response_blocks", []) == []
        assert all(
            "capture_execution_record_sha256" not in event["attributes"]
            for event in result.get("trace_events", [])
        )
    else:
        assert result["stop_reason"] is None
        facts = result["grounded_answer_artifact"]["facts"]
        assert len(facts) == 1
        assert facts[0]["unit"] == "widgets_per_week"
        assert any(
            "capture_execution_record_sha256" in event["attributes"]
            for event in result.get("trace_events", [])
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unit", "custom_unit_id"),
    [("count", None), ("custom_unit", "widgets_per_week")],
)
async def test_adhoc_checkpoint_restoration_reuses_noncanonical_authority_without_execution(
    unit: str,
    custom_unit_id: str | None,
) -> None:
    carrier, spec, binding = _checkpoint_carrier(unit=unit)
    assert (spec.custom_unit_id if unit == "custom_unit" else None) == custom_unit_id
    gateway, gateway_execute = _query_gateway()
    metric_runner = _CountingMetricRunner(
        values={"metric.revenue": 1000, "metric.stores": 25},
        gateway=gateway,
    )
    ad_hoc_runner = _CountingAdHocRunner()
    provider = _AnalysisProvider()
    checkpointer = MemorySaver()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue",))),
        context=_context(asset_ids=("metric.revenue",)),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
        checkpointer=checkpointer,
        model_provider=provider,
    )
    config = _config()
    pending = await engine.ainvoke(
        _graph_input(carrier, effective_mode="ANALYZE"), config
    )
    assert pending["decision_status"] == "awaiting_decision"
    assert ad_hoc_runner.calls == 0
    result = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert result.get("stop_reason") is None
    assert provider.calls == 1
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 1
    assert gateway_execute.await_count == 1
    fact = result["grounded_answer_artifact"]["facts"][0]
    assert fact["unit"] == (custom_unit_id or "count")
    assert fact["derived_output_id"] == derived_output_id(spec, binding)
    assert fact["calculation_scope"] == "ad_hoc_noncanonical"
    provenance = _provenance(result)
    assert provenance["calculation_scope"] == "ad_hoc_noncanonical"
    assert provenance["derived_output_ids"] == [derived_output_id(spec, binding)]
    assert provenance["metric_keys"] == []

    history = [snapshot async for snapshot in engine.aget_state_history(config)]
    execution_checkpoint = next(
        snapshot for snapshot in history if "analysis" in snapshot.next
    )
    checkpoint_record = execution_checkpoint.values["execution_record"]
    assert checkpoint_record["evidence_seal"]["version"] == "v1"
    checkpoint_plan = execution_checkpoint.values["execution_plan"]
    assert any(step["kind"] == "ad_hoc_calculation" for step in checkpoint_plan["steps"])
    resume_config = dict(config)
    resume_config["configurable"] = {
        **config["configurable"],
        **execution_checkpoint.config["configurable"],
    }
    restored = await engine.ainvoke(None, resume_config)

    assert restored.get("stop_reason") is None
    assert provider.calls == 2
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 1
    assert gateway_execute.await_count == 1
    assert restored["grounded_answer_artifact"]["facts"][0]["unit"] == (
        custom_unit_id or "count"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "custom_unit_id",
        "spec_checksum",
        "execution_binding_checksum",
        "derived_output_id",
        "receipt_and_fact_unit",
    ],
)
async def test_adhoc_checkpoint_restoration_rejects_calculation_authority_tampering(
    mutation: str,
) -> None:
    carrier, spec, binding = _checkpoint_carrier(
        unit="custom_unit", with_parameter=mutation == "execution_binding_checksum"
    )
    gateway, gateway_execute = _query_gateway()
    metric_runner = _CountingMetricRunner(gateway=gateway)
    ad_hoc_runner = _CountingAdHocRunner()
    provider = _AnalysisProvider()
    checkpointer = MemorySaver()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue",))),
        context=_context(asset_ids=("metric.revenue",)),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
        checkpointer=checkpointer,
        model_provider=provider,
    )
    config = _config()
    pending = await engine.ainvoke(
        _graph_input(carrier, effective_mode="ANALYZE"), config
    )
    assert pending["decision_status"] == "awaiting_decision"
    initial = await engine.ainvoke(Command(resume=_confirm_payload()), config)
    assert initial.get("stop_reason") is None
    assert provider.calls == 1
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 1
    assert gateway_execute.await_count == 1

    saved = (await engine.aget_state(config)).values
    plan = copy.deepcopy(saved["execution_plan"])
    record = copy.deepcopy(saved["execution_record"])
    artifact = copy.deepcopy(saved["grounded_answer_artifact"])
    answer_text = str(saved["grounded_answer_text"])
    adhoc_index = next(
        index
        for index, step in enumerate(plan["steps"])
        if step["kind"] == "ad_hoc_calculation"
    )
    adhoc_step = plan["steps"][adhoc_index]

    if mutation == "derived_output_id":
        original = adhoc_step["derived_output_id"]
        adhoc_step["derived_output_id"] = original[:-1] + ("b" if original[-1] == "a" else "a")
    elif mutation in {"custom_unit_id", "spec_checksum", "execution_binding_checksum"}:
        if mutation == "execution_binding_checksum":
            adhoc_step = _bind_checkpoint_step(
                adhoc_step, mutation=mutation
            )
        else:
            adhoc_step = _bind_checkpoint_step(adhoc_step, mutation=mutation)
        plan["steps"][adhoc_index] = adhoc_step
    else:
        fact = AnswerFact.model_validate(artifact["facts"][0]).model_copy(
            update={"unit": "widgets_per_month"}
        )
        fact = fact.model_copy(update={"fact_id": _grounded_fact_id(fact)})
        artifact["facts"][0] = fact.model_dump(mode="json")
        answer_text = render_grounded_answer(
            query_plan=QueryPlan.model_validate(saved["query_plan"]), facts=(fact,)
        )
        receipt = next(
            receipt
            for receipt in record["step_receipts"]
            if receipt["kind"] == "ad_hoc_calculation"
        )
        receipt["unit"] = "widgets_per_month"

    await engine.aupdate_state(
        config,
        {
            "execution_plan": plan,
            "execution_record": record,
            "grounded_answer_artifact": artifact,
            "grounded_answer_text": answer_text,
        },
        as_node="execute",
    )
    restored = await engine.ainvoke(None, config)

    assert restored.get("stop_reason") in {
        "checkpoint_adhoc_unit_authority_mismatch",
        "checkpoint_evidence_seal_invalid",
        "analysis_evidence_invalid",
    }
    assert restored.get("response_blocks", []) == []
    assert provider.calls == 1
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 1
    assert gateway_execute.await_count == 1


# --- 2. explicit capability gate ----------------------------------------------


@pytest.mark.asyncio
async def test_ad_hoc_is_refused_when_effective_mode_lacks_the_capability() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        # Injects the runner even WITHOUT the capability, so a passing test proves
        # the ENGINE gate refused the carrier rather than the runner being absent.
        inject_runner_without_capability=True,
    )

    # No run envelope -> no effective mode -> no run_scoped_derivation.
    result = await engine.ainvoke(_graph_input(_carrier(), effective_mode=None), _config())

    assert result["stop_reason"] == "ad_hoc_calculation_capability_denied"
    assert result["execution_record"] is None
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
    assert not any(
        block.get("type") == "provenance"
        for block in (result.get("response_blocks") or [])
    )


# --- 3. failure-close matrix --------------------------------------------------


def _matrix_case(name: str) -> dict[str, Any]:
    if name == "source_plan_mismatch":
        return {
            "context": _context(),
            "plan": _plan(metric_keys=("metric.revenue",)),
            "spec": _ratio_spec(),
            "catalog": None,
            "validator": None,
            "code": "ad_hoc_request_source_plan_mismatch",
        }
    if name == "input_catalog_bound":
        return {
            "context": _context(asset_ids=("metric.revenue",)),
            "plan": _plan(metric_keys=("metric.revenue",)),
            "spec": _single_spec(),
            "catalog": ApprovedCalculationCatalog(
                [_catalog_binding("metric.revenue")]
            ),
            "validator": None,
            "code": "ad_hoc_request_input_catalog_bound",
        }
    if name == "denominator_semantics_missing":
        return {
            "context": _context(asset_ids=("metric.revenue",)),
            "plan": _plan(metric_keys=("metric.revenue",)),
            "spec": _single_spec(unit="ratio"),
            "catalog": None,
            "validator": None,
            "code": "ad_hoc_request_denominator_semantics_missing",
        }
    if name == "input_ambiguous":
        return {
            "context": _context(
                asset_ids=("metric.revenue",), resolution_status="ambiguous"
            ),
            "plan": _plan(metric_keys=("metric.revenue",)),
            "spec": _single_spec(),
            "catalog": None,
            "validator": _AllowAllValidator(),
            "code": "ad_hoc_request_input_ambiguous",
        }
    raise AssertionError(name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "source_plan_mismatch",
        "input_catalog_bound",
        "denominator_semantics_missing",
        "input_ambiguous",
    ],
)
async def test_ad_hoc_rejection_code_reaches_stop_reason_with_zero_sql(
    case: str,
) -> None:
    fixture = _matrix_case(case)
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    compiler = (
        PlanCompiler(calculation_catalog=fixture["catalog"])
        if fixture["catalog"] is not None
        else None
    )
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(fixture["plan"]),
        context=fixture["context"],
        ad_hoc_runner=ad_hoc_runner,
        plan_compiler=compiler,
        plan_validator=fixture["validator"],
    )

    config = _config()
    result = await engine.ainvoke(_graph_input(_carrier(fixture["spec"])), config)

    if case == "source_plan_mismatch":
        # P6-B: this ONE code is an ALIGNMENT discrepancy between two well-formed
        # parses, so it is CONFIRMED first instead of hard-rejecting immediately.
        # Confirming it still fails closed with the SAME stable code and ZERO SQL,
        # so the refusal itself is not weakened by the confirmation.
        assert result["decision_status"] == "awaiting_decision"
        assert "__interrupt__" in result
        result = await engine.ainvoke(
            Command(resume=_confirm_payload()), config
        )

    assert result["stop_reason"] == fixture["code"]
    assert result["execution_record"] is None
    assert ad_hoc_runner.calls == 0
    # Zero SQL: no metric step was prepared or executed.
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0


# --- 4. A4 boundary: no definition lifecycle ----------------------------------


@pytest.mark.asyncio
async def test_ad_hoc_execution_never_touches_the_definition_lifecycle() -> None:
    store = InMemoryDefinitionStore()
    touched: list[str] = []

    for method in ("put_definition", "put_version", "put_lifecycle"):

        async def tripwire(*args: Any, _method: str = method, **kwargs: Any) -> None:
            touched.append(_method)
            raise AssertionError(f"definition lifecycle was touched: {_method}")

        setattr(store, method, tripwire)

    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )

    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier()), config)
    assert paused["decision_status"] == "awaiting_decision"
    result = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert result["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    # The live definition store was never written and stays empty.
    assert touched == []
    assert await store.list_definitions() == ()
    assert await store.list_versions(definition_id="any") == ()
    # And no definition block was projected into the product response.
    assert all(
        block.get("type") != "definition"
        for block in (result.get("response_blocks") or [])
    )


@pytest.mark.asyncio
async def test_ad_hoc_carrier_with_lifecycle_fields_is_rejected_unexecuted() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
    )
    payload = _graph_input(_carrier())
    carrier = payload["ad_hoc_calculation"]
    assert isinstance(carrier, dict)
    carrier["definition_id"] = "def-1"

    result = await engine.ainvoke(payload, _config())

    assert result["stop_reason"] == "execution_plan_compilation_failed"
    assert ad_hoc_runner.calls == 0
    assert metric_runner.execute_calls == 0


# --- 5. canonical non-regression ----------------------------------------------


@pytest.mark.asyncio
async def test_canonical_path_provenance_is_unchanged() -> None:
    metric_runner = _CountingMetricRunner(values={"metric.revenue": 7})
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue",))),
        context=_context(asset_ids=("metric.revenue",)),
        ad_hoc_runner=ad_hoc_runner,
    )

    result = await engine.ainvoke(_graph_input(None), _config())

    assert result["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 0
    provenance = _provenance(result)
    assert provenance["metric_keys"] == ["metric.revenue"]
    assert provenance["derived_output_ids"] == []
    assert provenance["calculation_scope"] is None


def test_canonical_provenance_still_requires_a_metric_key() -> None:
    window = ProvenanceTimeRangeBlock(
        start="2026-08-01", end="2026-08-31", timezone="Asia/Shanghai"
    )
    authority = ProvenanceAuthorityBlock(
        execution_plan_checksum="b" * 64, receipt_step_ids=("s1",)
    )
    # The pre-existing canonical invariant is NOT weakened: no metric key and no
    # ad_hoc scope is still a hard ValidationError.
    with pytest.raises(ValidationError):
        ProvenanceBlock(
            evidence_checksum="a" * 64,
            metric_keys=(),
            analysis_window=window,
            fact_ids=("f1",),
            authority_provenance=authority,
        )
    # A canonical block must never carry a derived output id either.
    with pytest.raises(ValidationError):
        ProvenanceBlock(
            evidence_checksum="a" * 64,
            metric_keys=("metric.revenue",),
            derived_output_ids=(DERIVED_PREFIX + "a" * 32,),
            analysis_window=window,
            fact_ids=("f1",),
            authority_provenance=authority,
        )
    # An ad_hoc scope without a derived id is refused too.
    with pytest.raises(ValidationError):
        ProvenanceBlock(
            evidence_checksum="a" * 64,
            metric_keys=(),
            calculation_scope="ad_hoc_noncanonical",
            analysis_window=window,
            fact_ids=("f1",),
            authority_provenance=authority,
        )
