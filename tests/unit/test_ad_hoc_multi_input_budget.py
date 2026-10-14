"""P2-A: multi-input run-scoped AD_HOC is REACHABLE under the DEFAULT budget.

Execution-level evidence through the REAL compiled LangGraph engine:

* a 2-input ratio AD_HOC executes end to end under the DEFAULT fast route
  (execution_record.status == "succeeded", the shared arithmetic runner runs
  exactly once, the grounded result is non-empty, and provenance carries
  calculation_scope == "ad_hoc_noncanonical");
* a 3-input AD_HOC executes too: the allowance is derived from the carrier's
  OWN declared inputs, not hardcoded to 2;
* the allowance is AUDITED exactly once with an explicit trace event;
* a plain (non-AD_HOC) canonical calculation keeps the UNCHANGED fast limits
  and is still DENIED when its plan needs two dependency fetches;
* a carrier declaring more inputs than any executable AD_HOC plan can hold fails
  closed with zero SQL executed, and the allowance is clamped to that ceiling.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from src.nl2sql.agents.dynamic_calc.trusted_templates import trusted_template_registry
from src.nl2sql.contracts import (
    AuthorizationContext,
    ContextBundle,
    ExecutionReceipt,
    QueryPlan,
    RequestContext,
    RequestIdentity,
    TimeRange,
    evaluate_authorization,
)
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.ad_hoc_request import AdHocCalculationRequest
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
    ApprovedCalculationInput,
)
from src.nl2sql.orchestration.budget import bootstrap_routing_budget_policy
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
    RuntimeCalculationRunner,
)
from src.nl2sql.orchestration.planning import PlanCompiler, PlanValidator
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
    derived_output_id,
)

RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
REQUEST_ID = UUID("33333333-3333-3333-3333-333333333333")
THREAD_ID = UUID("44444444-4444-4444-4444-444444444444")
ALLOWANCE_EVENT = "ad_hoc_dependency_budget_allowance"
# The executable AD_HOC plan ceiling: N dependency fetches + calculation +
# verify must fit the compiler 16-step shape (N + 2 <= 16).
EXECUTABLE_INPUT_CEILING = 14


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


def _context(*, asset_ids: tuple[str, ...]) -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE_ID,
        schema_snapshot_id=SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=asset_ids,
        resolution_status="resolved",
    )


def _plan(*, metric_keys: tuple[str, ...]) -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=metric_keys,
        time_range=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=("metrics:read",),
    )


def _ratio_spec(
    metric_keys: tuple[str, ...] = ("metric.revenue", "metric.stores"),
) -> CalculationSpec:
    """A legal 2-input ratio AD_HOC spec over uniquely resolved inputs."""

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


def _three_input_spec(
    metric_keys: tuple[str, ...] = ("metric.a", "metric.b", "metric.c"),
) -> CalculationSpec:
    """A legal 3-input AD_HOC spec: first + second + third."""

    return CalculationSpec(
        calculation_id="adhoc.triple_sum",
        expression=BinaryOperand(
            op="add",
            left=InputRefOperand(role="first"),
            right=BinaryOperand(
                op="add",
                left=InputRefOperand(role="second"),
                right=InputRefOperand(role="third"),
            ),
        ),
        inputs=(
            CalculationInputSpec(
                role="first", provenance="published_gold", metric_key=metric_keys[0]
            ),
            CalculationInputSpec(
                role="second", provenance="published_gold", metric_key=metric_keys[1]
            ),
            CalculationInputSpec(
                role="third", provenance="published_gold", metric_key=metric_keys[2]
            ),
        ),
        unit="count",
        precision=2,
        rounding="half_up",
    )


def _wide_spec(count: int) -> CalculationSpec:
    """A spec declaring `count` inputs, each consumed by the expression."""

    roles = tuple(f"role_{index:02d}" for index in range(count))
    metric_keys = tuple(f"metric.m{index:02d}" for index in range(count))
    expression: Any = InputRefOperand(role=roles[0])
    for role in roles[1:]:
        expression = BinaryOperand(
            op="add", left=expression, right=InputRefOperand(role=role)
        )
    return CalculationSpec(
        calculation_id="adhoc.wide",
        expression=expression,
        inputs=tuple(
            CalculationInputSpec(
                role=role, provenance="published_gold", metric_key=metric_key
            )
            for role, metric_key in zip(roles, metric_keys)
        ),
        unit="count",
        precision=2,
        rounding="half_up",
    )


def _binding(spec: CalculationSpec) -> CalculationExecutionBinding:
    return CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(),
    )


def _carrier(spec: CalculationSpec) -> AdHocCalculationRequest:
    return AdHocCalculationRequest(
        calculation_spec=spec, execution_binding=_binding(spec)
    )


def _canonical_catalog(
    *, canonical_key: str, input_keys: tuple[str, ...]
) -> ApprovedCalculationCatalog:
    meta = trusted_template_registry.metadata("ratio")
    binding = ApprovedCalculationBinding(
        canonical_metric_key=canonical_key,
        template_id="ratio",
        template_version=meta.version,
        template_checksum=meta.checksum,
        inputs=tuple(
            ApprovedCalculationInput(
                role=role, metric_key=key, metric_contract_sha256="b" * 64
            )
            for role, key in zip(("numerator", "denominator"), input_keys)
        ),
        binding_revision="1",
        semantic_release_id=str(RELEASE_ID),
        semantic_release_checksum="c" * 64,
    )
    return ApprovedCalculationCatalog([binding])


# --- runtime doubles ----------------------------------------------------------


class _CountingMetricRunner:
    """Counts every dependency/direct fetch prepared and executed.

    Each metric key yields a DISTINCT SQL fingerprint so the route ledger's
    repeated-SQL guard does not stand in for real execution evidence.
    """

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
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
    ) -> None:
        self.metric_runner = metric_runner
        self.provider = provider
        self.context = context
        self.ad_hoc_runner = ad_hoc_runner
        self.capabilities_seen: list[frozenset[str]] = []

    async def __call__(
        self,
        *,
        identity: RequestIdentity,
        authorization: AuthorizationContext | None,
        expected_revision: str | None,
        capabilities: frozenset[str] = frozenset(),
    ) -> Any:
        del expected_revision
        self.capabilities_seen.append(frozenset(capabilities))
        if authorization is None:
            return TypedRuntimeUnavailable(reason="authorization_context_missing")
        gated = ad_hoc_calculation_runner_for_capabilities(capabilities)
        runner = self.ad_hoc_runner if gated is not None else None
        return _StubRuntime(
            context_resolver=_Resolver(self.context),
            query_plan_provider=self.provider,
            plan_executor=PlanExecutor(
                metric_runner=self.metric_runner,
                ad_hoc_calculation_runner=runner,
            ),
            identity=identity,
            authorization=authorization,
            authorization_revision=authorization.authorization_revision,
        )


class _Provider:
    provider_name = "synthetic"

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        del kwargs
        return ProviderResponse(content="x", model="small", usage={}, finish_reason="stop")

    async def list_models(self) -> tuple[str, ...]:
        return ("small",)


def _gateway() -> ModelGateway:
    return ModelGateway(
        providers={"synthetic": _Provider()},
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


def _config(revision: str = "rev-1") -> dict[str, Any]:
    return runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_ID,
            trace_id="trace-adhoc-budget",
            deadline_ms=10_000,
            authorization=_authorization(revision),
        )
    )


def _graph_input(carrier: AdHocCalculationRequest | None = None) -> dict[str, Any]:
    from src.nl2sql.orchestration.mode_contract import RunEnvelope

    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "revenue per store"}]
    }
    envelope = RunEnvelope(
        run_id="run-adhoc-budget",
        requested_mode="QUERY",
        effective_mode="QUERY",
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
    plan_compiler: PlanCompiler | None = None,
    plan_validator: PlanValidator | None = None,
) -> tuple[Any, _RuntimeFactory]:
    factory = _RuntimeFactory(
        metric_runner=metric_runner,
        provider=provider,
        context=context,
        ad_hoc_runner=ad_hoc_runner,
    )
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_gateway(),
        plan_compiler=plan_compiler,
        plan_validator=plan_validator,
        typed_runtime_factory=factory,
    )
    return engine, factory


def _confirm_payload(key: str = "k-confirm") -> dict[str, object]:
    """The typed confirm of the P6-B explicit-formula plan confirmation."""

    return {"action": "confirm", "idempotency_key": key}


def _provenance(state: dict[str, Any]) -> dict[str, Any]:
    for block in state.get("response_blocks") or []:
        if isinstance(block, dict) and block.get("type") == "provenance":
            return block
    raise AssertionError("no provenance block was emitted")


def _allowance_events(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in state.get("trace_events") or []
        if isinstance(event, dict) and event.get("name") == ALLOWANCE_EVENT
    ]


# --- 1. multi-input end to end under the DEFAULT fast route --------------------


@pytest.mark.asyncio
async def test_two_input_ratio_ad_hoc_executes_under_the_default_fast_route() -> None:
    metric_runner = _CountingMetricRunner(
        {"metric.revenue": 1000, "metric.stores": 25}
    )
    ad_hoc_runner = _CountingAdHocRunner()
    spec = _ratio_spec()
    engine, factory = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue", "metric.stores"))),
        context=_context(asset_ids=("metric.revenue", "metric.stores")),
        ad_hoc_runner=ad_hoc_runner,
    )

    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier(spec)), config)
    # P6-B: the explicit formula is confirmed ONCE before anything executes.
    assert paused["decision_status"] == "awaiting_decision"
    assert ad_hoc_runner.calls == 0
    result = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert result["route_record"]["route"] == "fast"
    assert result["stop_reason"] is None
    assert result["execution_record"]["status"] == "succeeded"
    # The shared arithmetic runner ran EXACTLY ONCE, on the resolved scalars.
    assert ad_hoc_runner.calls == 1
    assert ad_hoc_runner.last_inputs == {"numerator": 1000, "denominator": 25}
    # BOTH dependency fetches really executed; nothing was silently dropped.
    assert metric_runner.prepare_calls == 2
    assert metric_runner.execute_calls == 2
    assert "40.0000" in result["grounded_answer_text"]
    assert result["budget_record"]["usage"]["sql_executions"] == 2
    provenance = _provenance(result)
    assert provenance["calculation_scope"] == "ad_hoc_noncanonical"
    assert provenance["derived_output_ids"] == [derived_output_id(spec, _binding(spec))]
    assert provenance["metric_keys"] == []
    assert "run_scoped_derivation" in factory.capabilities_seen[0]


@pytest.mark.asyncio
async def test_three_input_ad_hoc_executes_under_the_default_fast_route() -> None:
    metric_keys = ("metric.a", "metric.b", "metric.c")
    metric_runner = _CountingMetricRunner(
        {"metric.a": 1, "metric.b": 2, "metric.c": 3}
    )
    ad_hoc_runner = _CountingAdHocRunner()
    spec = _three_input_spec(metric_keys)
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=metric_keys)),
        context=_context(asset_ids=metric_keys),
        ad_hoc_runner=ad_hoc_runner,
    )

    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier(spec)), config)
    assert paused["decision_status"] == "awaiting_decision"
    result = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert result["route_record"]["route"] == "fast"
    assert result["stop_reason"] is None
    assert result["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert ad_hoc_runner.last_inputs == {"first": 1, "second": 2, "third": 3}
    assert metric_runner.execute_calls == 3
    assert "6.00" in result["grounded_answer_text"]
    assert result["budget_record"]["usage"]["sql_executions"] == 3
    provenance = _provenance(result)
    assert provenance["calculation_scope"] == "ad_hoc_noncanonical"


# --- 2. auditability of the allowance -----------------------------------------


@pytest.mark.asyncio
async def test_ad_hoc_allowance_is_audited_once_with_the_raised_limits() -> None:
    metric_runner = _CountingMetricRunner(
        {"metric.revenue": 1000, "metric.stores": 25}
    )
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue", "metric.stores"))),
        context=_context(asset_ids=("metric.revenue", "metric.stores")),
        ad_hoc_runner=ad_hoc_runner,
    )

    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier(_ratio_spec())), config)
    assert paused["decision_status"] == "awaiting_decision"
    # The run-scoped allowance is already AUDITED once at the compile gate, before
    # the confirmation, and must not be duplicated by the continuation.
    assert len(_allowance_events(paused)) == 1
    result = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert result["execution_record"]["status"] == "succeeded"
    events = _allowance_events(result)
    assert len(events) == 1, "the allowance must be audited exactly once"
    attributes = events[0]["attributes"]
    assert attributes["route"] == "fast"
    assert attributes["declared_inputs"] == 2
    assert attributes["granted_sql_candidates"] == 2
    assert attributes["granted_sql_executions"] == 2
    assert attributes["base_sql_candidates"] == 1
    assert attributes["base_sql_executions"] == 1
    # The versioned policy limits are NOT persisted as raised: the checkpoint
    # keeps the policy envelope and the allowance is re-derived from the carrier.
    assert result["budget_record"]["limits"]["max_sql_candidates"] == 1
    assert result["budget_record"]["limits"]["max_sql_executions"] == 1


@pytest.mark.asyncio
async def test_plain_query_and_single_input_ad_hoc_get_no_allowance() -> None:
    # The frozen bootstrap policy table itself is unchanged.
    fast = bootstrap_routing_budget_policy().routes["fast"]
    assert fast.max_sql_candidates == 1
    assert fast.max_sql_executions == 1

    plain_runner = _CountingMetricRunner({"metric.revenue": 7})
    plain_engine, _ = _engine(
        metric_runner=plain_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue",))),
        context=_context(asset_ids=("metric.revenue",)),
        ad_hoc_runner=_CountingAdHocRunner(),
    )
    plain = await plain_engine.ainvoke(_graph_input(None), _config())
    assert plain["execution_record"]["status"] == "succeeded"
    assert plain["route_record"]["route"] == "fast"
    assert _allowance_events(plain) == []
    assert plain["budget_record"]["limits"]["max_sql_executions"] == 1


# --- 3. regression: a PLAIN plan still obeys the fast budget -------------------


@pytest.mark.asyncio
async def test_plain_two_fetch_plan_is_still_denied_by_the_fast_budget() -> None:
    """A non-AD_HOC canonical calculation needing TWO fetches is still denied.

    This is the regression guard for the whole change: the fast route keeps
    max_sql_candidates == max_sql_executions == 1, so a plain plan that needs two
    dependency fetches is refused before any SQL runs.
    """

    canonical_key = "metric.revenue_per_store"
    input_keys = ("metric.revenue", "metric.stores")
    catalog = _canonical_catalog(canonical_key=canonical_key, input_keys=input_keys)
    metric_runner = _CountingMetricRunner({})
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=(canonical_key,))),
        context=_context(asset_ids=(canonical_key, *input_keys)),
        ad_hoc_runner=ad_hoc_runner,
        plan_compiler=PlanCompiler(calculation_catalog=catalog),
        plan_validator=PlanValidator(calculation_catalog=catalog),
    )

    result = await engine.ainvoke(_graph_input(None), _config())

    assert result["route_record"]["route"] == "fast"
    assert result["execution_record"] is None
    assert result["stop_reason"] == "execution_plan_validation_denied"
    codes = {
        issue["code"]
        for issue in result["execution_plan_validation"]["issues"]
    }
    assert "execution_plan_sql_candidate_budget_exceeded" in codes
    assert "execution_plan_sql_execution_budget_exceeded" in codes
    assert result["execution_plan_validation"]["outcome"] == "deny"
    # Zero SQL and no AD_HOC arithmetic.
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
    assert ad_hoc_runner.calls == 0
    assert _allowance_events(result) == []


# --- 4. bounded allowance and fail-closed over-limit --------------------------


def test_carrier_input_bound_is_the_models_own_constraint() -> None:
    """A declaration beyond CalculationSpec.inputs max_length is refused by the model.

    The allowance can therefore never be driven by an arbitrary client number:
    only a fully parsed carrier reaches it, and the model caps inputs at 32.
    """

    with pytest.raises(ValidationError):
        CalculationSpec(
            calculation_id="adhoc.too_wide",
            expression=InputRefOperand(role="role_00"),
            inputs=tuple(
                CalculationInputSpec(
                    role=f"role_{index:02d}",
                    provenance="published_gold",
                    metric_key=f"metric.m{index:02d}",
                )
                for index in range(33)
            ),
            unit="count",
        )


@pytest.mark.asyncio
async def test_declared_inputs_beyond_the_executable_ceiling_fail_closed() -> None:
    """15 declared inputs: the model accepts it, no executable AD_HOC plan can.

    The allowance is clamped to the executable ceiling (14), the compiler refuses
    the shape, and NOTHING is executed.
    """

    declared = EXECUTABLE_INPUT_CEILING + 1
    spec = _wide_spec(declared)
    metric_keys = tuple(item.metric_key for item in spec.inputs if item.metric_key)
    metric_runner = _CountingMetricRunner({})
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=metric_keys)),
        context=_context(asset_ids=metric_keys),
        ad_hoc_runner=ad_hoc_runner,
    )

    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier(spec)), config)
    # P6-B: the formula is well formed, so it is CONFIRMED once and only the
    # confirmed compile discovers that no executable AD_HOC plan can hold it.
    assert paused["decision_status"] == "awaiting_decision"
    result = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert result["execution_record"] is None
    assert result["stop_reason"] == "execution_plan_compilation_failed"
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
    assert ad_hoc_runner.calls == 0
    # The audit event proves the clamp: 15 declared, 14 granted.
    events = _allowance_events(result)
    assert len(events) == 1
    assert events[0]["attributes"]["declared_inputs"] == declared
    assert (
        events[0]["attributes"]["granted_sql_executions"]
        == EXECUTABLE_INPUT_CEILING
    )


@pytest.mark.asyncio
async def test_carrier_with_a_raw_oversized_input_list_never_reaches_execution() -> None:
    """A raw client carrier past the model bound is rejected before any SQL."""

    metric_runner = _CountingMetricRunner({})
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue",))),
        context=_context(asset_ids=("metric.revenue",)),
        ad_hoc_runner=ad_hoc_runner,
    )
    payload = _graph_input(_carrier(_ratio_spec(("metric.revenue", "metric.revenue"))))
    carrier = payload["ad_hoc_calculation"]
    assert isinstance(carrier, dict)
    spec = carrier["calculation_spec"]
    assert isinstance(spec, dict)
    # 40 declared inputs, all referenced by a flat OR-free chain is not needed:
    # the model rejects the DECLARED count itself before any semantic check.
    spec["inputs"] = [
        {
            "role": f"role_{index:02d}",
            "provenance": "published_gold",
            "metric_key": f"metric.m{index:02d}",
        }
        for index in range(40)
    ]

    result = await engine.ainvoke(payload, _config())

    assert result["execution_record"] is None
    assert result["stop_reason"] == "execution_plan_compilation_failed"
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
    assert ad_hoc_runner.calls == 0
    assert _allowance_events(result) == []

# --- 5. causal proof: the allowance IS the gate that flips the outcome --------


def test_the_allowance_is_what_makes_the_two_input_plan_admissible() -> None:
    """The SAME compiled plan is denied at 1/1 and allowed at the raised limits.

    This drives the real compiler, the real validator and the real budget
    derivation point, so it proves the mechanism rather than only the outcome.
    """

    from src.nl2sql.orchestration.engine import _route_budget_from_state

    spec = _ratio_spec()
    carrier = _carrier(spec)
    context = _context(asset_ids=("metric.revenue", "metric.stores"))
    plan = _plan(metric_keys=("metric.revenue", "metric.stores"))
    validator = PlanValidator()
    query_validation = validator.validate_query_plan(
        plan=plan, context=context, identity=_identity()
    )
    assert query_validation.outcome == "allow"
    execution_plan = PlanCompiler().compile_ad_hoc(
        plan=plan,
        context=context,
        validation=query_validation,
        calculation_spec=spec,
        execution_binding=_binding(spec),
    )

    policy = bootstrap_routing_budget_policy()
    base = policy.routes["fast"]
    assert (base.max_sql_candidates, base.max_sql_executions) == (1, 1)
    denied = validator.validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=base,
    )
    assert denied.outcome == "deny"
    assert {
        issue.code for issue in denied.issues
    } >= {
        "execution_plan_sql_candidate_budget_exceeded",
        "execution_plan_sql_execution_budget_exceeded",
    }

    derived = _route_budget_from_state(
        {"ad_hoc_calculation": carrier.model_dump(mode="json")},
        route="fast",
        policy=policy,
    )
    assert derived.limits.max_sql_candidates == 2
    assert derived.limits.max_sql_executions == 2
    # Every OTHER fast limit is untouched by the allowance.
    assert derived.limits.max_join_hops == base.max_join_hops == 0
    assert derived.limits.max_model_calls == base.max_model_calls == 1
    assert derived.limits.max_repairs == base.max_repairs == 0
    allowed = validator.validate_execution_plan(
        execution_plan=execution_plan,
        query_plan=plan,
        context=context,
        route_budget=derived.limits,
    )
    assert allowed.outcome == "allow", allowed.issues
