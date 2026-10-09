"""P6-B explicit-formula confirmation + §4.3 human-wait deadline accounting.

These tests drive the REAL compiled LangGraph engine (not a node in isolation)
and prove, with execution-level evidence, that:

* an EXPLICIT formula carrier ALWAYS suspends as ONE typed
  metric_plan_confirmation (issue code custom_metric_plan_confirmation) whose
  safe_summary names BOTH the formula-declared inputs and the
  question-resolved inputs, and which is the ONLY kind that can carry an
  in-place correction (resolve) - never "modify";
* confirm continues into the governed AD_HOC compile/execute path EXACTLY
  ONCE, reject/cancel execute ZERO times, and a repeated idempotency key does
  not execute twice;
* an ALIGNMENT discrepancy is confirmable (and a confirmed misalignment still
  fails closed with the stable request-entry code), while structural /
  authority-class request-entry refusals stay hard stops BEFORE any
  confirmation;
* a plain QUERY with NO formula never suspends;
* §4.3: human waiting time is deducted from the calculation deadline, an
  already-consumed budget is never reset, and a MISSING wait record deducts
  nothing (the stricter, un-deducted deadline applies).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql import v2 as v2_module
from src.nl2sql.agents.dynamic_calc.trusted_templates import trusted_template_registry
from src.nl2sql.contracts import (
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
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration import engine as engine_module
from src.nl2sql.orchestration.ad_hoc_request import AdHocCalculationRequest
from src.nl2sql.orchestration.approved_compute import (
    ApprovedCalculationBinding,
    ApprovedCalculationCatalog,
    ApprovedCalculationInput,
)
from src.nl2sql.orchestration.decision_contract import HITLDecision, revalidate_request
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
    RuntimeCalculationRunner,
)
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
    derived_output_id,
)
from src.nl2sql.v2 import register_v2_routes

RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
REQUEST_ID = UUID("33333333-3333-3333-3333-333333333333")
THREAD_ID = UUID("44444444-4444-4444-4444-444444444444")


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
    unresolved_slots: tuple[str, ...] = (),
) -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=metric_keys,
        time_range=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=("metrics:read",),
        unresolved_slots=unresolved_slots,
    )


def _ratio_spec(
    *, metric_keys: tuple[str, ...] = ("metric.revenue", "metric.stores")
) -> CalculationSpec:
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


def _single_spec(*, metric_key: str = "metric.revenue", unit: str = "count") -> CalculationSpec:
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
    """Counts every dependency/direct fetch prepared and executed."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = values or {"metric.revenue": 1000, "metric.stores": 25}
        self.prepare_calls = 0
        self.execute_calls = 0

    @staticmethod
    def _fingerprint(metric_key: str) -> str:
        return hashlib.sha256(metric_key.encode("utf-8")).hexdigest()

    async def prepare(self, *, step: Any, query_plan: Any, context: Any) -> PreparedMetricStep:
        del query_plan, context
        self.prepare_calls += 1
        metric_key = step.metric_keys[0]
        return PreparedMetricStep(
            sql_fingerprint=self._fingerprint(metric_key),
            join_hops=0,
            payload={"sql": "SELECT synthetic", "metric_key": metric_key},
            dependency_fetch=step.ad_hoc_input_role is not None,
        )

    async def execute(self, prepared: PreparedMetricStep, *, timeout_ms: int) -> MetricStepResult:
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

    async def resolve(self, *, question: str, identity: RequestIdentity, route_hint: str) -> ContextBundle:
        del question, identity, route_hint
        return self._context


class _StaticPlanProvider:
    is_deterministic = True

    def __init__(self, plan: QueryPlan) -> None:
        self._plan = plan

    async def propose(self, *, question: str, context: ContextBundle, identity: RequestIdentity) -> QueryPlan:
        del question, context, identity
        return self._plan


class _SlotReplanProvider:
    """A deterministic provider that can resolve a slot into a NEW plan."""

    is_deterministic = True

    def __init__(self, plan: QueryPlan, replanned: QueryPlan) -> None:
        self._plan = plan
        self._replanned = replanned
        self.slot_calls = 0

    async def propose(self, *, question: str, context: ContextBundle, identity: RequestIdentity) -> QueryPlan:
        del question, context, identity
        return self._plan

    async def replan_with_slot_bindings(
        self,
        *,
        question: str,
        context: ContextBundle,
        identity: RequestIdentity,
        base_plan: QueryPlan,
        slot_bindings: Any,
    ) -> QueryPlan:
        del question, context, identity
        self.slot_calls += 1
        bound = {binding.slot for binding in slot_bindings}
        unresolved = [
            slot for slot in base_plan.unresolved_slots if slot not in bound
        ]
        return self._replanned.model_copy(
            update={"unresolved_slots": tuple(unresolved)}
        )


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
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        identity: RequestIdentity,
        authorization: AuthorizationContext | None,
        expected_revision: str | None,
        capabilities: frozenset[str] = frozenset(),
    ) -> Any:
        self.capabilities_seen.append(frozenset(capabilities))
        self.calls.append(
            {
                "revision": getattr(authorization, "authorization_revision", None),
                "expected_revision": expected_revision,
            }
        )
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


class _AllowAllValidator:
    """Forces an allow query-plan validation so the AD_HOC resolver is reached."""

    policy_version = "plan-validator.test.allow"
    policy_checksum = "f" * 64

    def validate_query_plan(self, *, plan: QueryPlan, context: ContextBundle, identity: RequestIdentity) -> PlanValidationRecord:
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
            "plan.standard": ModelProfile(
                "plan.standard",
                "test-v1",
                frozenset({"plan"}),
                ModelTarget("synthetic", "small", "small"),
                None,
            ),
        },
    )


def _deep_route_policy() -> RoutePolicy:
    """A policy that sends the legacy typed plan down the deep route."""

    return RoutePolicy(
        version="route.test.deep",
        state="bootstrap",
        fast_max_risk=0,
        fast_min_confidence=1.0,
        fast_max_tables=1,
        standard_max_risk=0,
        standard_min_confidence=1.0,
    )


def _legacy_deep_engine() -> tuple[Any, _CountingMetricRunner]:
    """The compatibility (static-collaborator) engine whose deep route suspends.

    It has NO request-scoped factory, exactly like the pre-existing legacy HITL
    path, so the deep model call is consumed BEFORE the approval suspension.
    """

    runner = _CountingMetricRunner({"metric.revenue": 4242})
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_gateway(),
        route_policy=_deep_route_policy(),
        context_resolver=_Resolver(_context()),
        query_plan_provider=_StaticPlanProvider(
            _plan(metric_keys=("metric.revenue",))
        ),
        plan_executor=PlanExecutor(metric_runner=runner),
    )
    return engine, runner


def _standard_route_policy() -> RoutePolicy:
    return RoutePolicy(
        version="route.test.standard-only",
        state="bootstrap",
        fast_max_risk=20,
        fast_min_confidence=1.0,
        fast_max_tables=1,
        standard_max_risk=60,
        standard_min_confidence=0.55,
    )


def _config(revision: str = "rev-1", *, deadline_ms: int = 10_000) -> dict[str, Any]:
    return runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_ID,
            trace_id="trace-p6b",
            deadline_ms=deadline_ms,
            authorization=_authorization(revision),
        )
    )


def _graph_input(
    carrier: AdHocCalculationRequest | None = None,
    *,
    effective_mode: str | None = "QUERY",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"messages": [{"role": "user", "content": "revenue per store"}]}
    if effective_mode is not None:
        from src.nl2sql.orchestration.mode_contract import RunEnvelope

        envelope = RunEnvelope(
            run_id="run-p6b",
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
        route_policy=route_policy,
        plan_compiler=plan_compiler,
        plan_validator=plan_validator,
        typed_runtime_factory=factory,
    )
    return engine, factory


def _confirm_payload(key: str = "k-confirm") -> dict[str, object]:
    return {"action": "confirm", "idempotency_key": key}


def _provenance(state: dict[str, Any]) -> dict[str, Any]:
    for block in state.get("response_blocks") or []:
        if isinstance(block, dict) and block.get("type") == "provenance":
            return block
    raise AssertionError("no provenance block was emitted")


class _FakeClock:
    """A virtual UTC wall clock, so a long human wait costs no test wall time."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)

def _matrix_case(name: str) -> dict[str, Any]:
    if name == "input_catalog_bound":
        return {
            "context": _context(asset_ids=("metric.revenue",)),
            "plan": _plan(metric_keys=("metric.revenue",)),
            "spec": _single_spec(),
            "catalog": ApprovedCalculationCatalog([_catalog_binding("metric.revenue")]),
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
            "context": _context(asset_ids=("metric.revenue",), resolution_status="ambiguous"),
            "plan": _plan(metric_keys=("metric.revenue",)),
            "spec": _single_spec(),
            "catalog": None,
            "validator": _AllowAllValidator(),
            "code": "ad_hoc_request_input_ambiguous",
        }
    raise AssertionError(name)


# --- deliverable 1: an explicit formula is ALWAYS confirmed once ---------------


@pytest.mark.asyncio
async def test_explicit_formula_suspends_as_one_typed_confirmation() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )

    paused = await engine.ainvoke(_graph_input(_carrier()), _config())

    assert "__interrupt__" in paused
    assert paused["stop_reason"] is None
    assert paused["decision_status"] == "awaiting_decision"
    assert paused["decision_version"] == 1
    assert paused["ad_hoc_confirmation_satisfied"] is False
    request = revalidate_request(paused["pending_decision"])
    # P7: the suspension is the ONE correction kind.  Its action set is the
    # FROZEN correction vocabulary - "modify" is deliberately absent, so there is
    # exactly ONE way to correct (resolve) and no payload-less second "change".
    assert request.decision_kind == "metric_plan_confirmation"
    assert request.issue_codes == ("custom_metric_plan_confirmation",)
    assert request.allowed_actions == ("confirm", "resolve", "reject", "cancel")
    assert request.unresolved_slots == ()
    assert request.resolution_options
    summary = request.safe_summary
    assert isinstance(summary, str) and len(summary) <= 512
    # BOTH sides are named: formula-declared (role=metric) and question-resolved.
    assert "numerator=metric.revenue" in summary
    assert "denominator=metric.stores" in summary
    assert "metric.revenue, metric.stores" in summary
    assert summary.endswith("| aligned")
    # Nothing was compiled or executed while suspended.
    assert paused["execution_plan"] is None
    assert paused["execution_record"] is None
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0


@pytest.mark.asyncio
async def test_confirmed_formula_executes_exactly_once() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    spec = _ratio_spec()
    binding = _binding(spec)
    engine, factory = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )
    config = _config()

    paused = await engine.ainvoke(_graph_input(_carrier(spec, binding)), config)
    assert paused["decision_status"] == "awaiting_decision"

    resumed = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert resumed["decision_status"] == "resolved_continuing_current_authorization"
    assert resumed["continuation_ready"] is True
    assert resumed["ad_hoc_confirmation_satisfied"] is True
    assert resumed["pending_decision"] is None
    assert resumed["stop_reason"] is None
    assert resumed["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert ad_hoc_runner.last_inputs == {"numerator": 1000, "denominator": 25}
    assert metric_runner.prepare_calls == 2
    assert metric_runner.execute_calls == 2
    assert "40.0000" in resumed["grounded_answer_text"]
    provenance = _provenance(resumed)
    assert provenance["calculation_scope"] == "ad_hoc_noncanonical"
    assert provenance["derived_output_ids"] == [derived_output_id(spec, binding)]
    assert provenance["metric_keys"] == []
    assert factory.capabilities_seen
    assert "run_scoped_derivation" in factory.capabilities_seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "status"), [("reject", "rejected"), ("cancel", "cancelled")])
async def test_rejected_or_cancelled_formula_executes_zero_times(action: str, status: str) -> None:
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

    resumed = await engine.ainvoke(
        Command(resume={"action": action, "idempotency_key": "k-" + action}), config
    )

    assert resumed["decision_status"] == status
    assert resumed["pending_decision"] is None
    assert resumed["ad_hoc_confirmation_satisfied"] is False
    assert resumed["execution_record"] is None
    assert resumed.get("continuation_ready") is not True
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0


@pytest.mark.asyncio
async def test_repeated_confirm_idempotency_key_does_not_execute_twice() -> None:
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
    await engine.ainvoke(_graph_input(_carrier()), config)

    first = await engine.ainvoke(Command(resume=_confirm_payload("k-1")), config)
    assert first["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2

    second = await engine.ainvoke(Command(resume=_confirm_payload("k-1")), config)
    assert second["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2


@pytest.mark.asyncio
async def test_plain_query_without_any_formula_never_suspends() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )

    result = await engine.ainvoke(_graph_input(None), _config())

    assert "__interrupt__" not in result
    assert result["decision_status"] is None
    assert result["pending_decision"] is None
    assert result["ad_hoc_confirmation_satisfied"] is False
    assert result["stop_reason"] is None
    assert result["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 0
    provenance = _provenance(result)
    assert provenance["metric_keys"] == ["metric.revenue", "metric.stores"]
    assert provenance["calculation_scope"] is None


@pytest.mark.asyncio
async def test_misaligned_formula_is_confirmed_not_hard_rejected() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue",))),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )
    config = _config()

    paused = await engine.ainvoke(_graph_input(_carrier()), config)

    # The ALIGNMENT discrepancy is CONFIRMABLE, not a hard rejection.
    assert paused["decision_status"] == "awaiting_decision"
    assert "__interrupt__" in paused
    request = revalidate_request(paused["pending_decision"])
    assert request.safe_summary is not None
    assert "numerator=metric.revenue" in request.safe_summary
    assert "denominator=metric.stores" in request.safe_summary
    assert "Question-resolved inputs: metric.revenue" in request.safe_summary
    assert request.safe_summary.endswith("| MISMATCH")
    assert ad_hoc_runner.calls == 0

    resumed = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    # Confirming a misalignment does NOT override the request-entry refusal.
    assert resumed["decision_status"] == "ad_hoc_confirmation_rejected"
    assert resumed["stop_reason"] == "ad_hoc_request_source_plan_mismatch"
    assert resumed["execution_record"] is None
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0


@pytest.mark.asyncio
async def test_misaligned_formula_can_be_rejected_without_any_execution() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan(metric_keys=("metric.revenue",))),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )
    config = _config()
    await engine.ainvoke(_graph_input(_carrier()), config)

    resumed = await engine.ainvoke(
        Command(resume={"action": "reject", "idempotency_key": "k-mismatch-reject"}),
        config,
    )

    assert resumed["decision_status"] == "rejected"
    assert resumed["execution_record"] is None
    assert ad_hoc_runner.calls == 0
    assert metric_runner.execute_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["input_catalog_bound", "denominator_semantics_missing", "input_ambiguous"],
)
async def test_structural_refusals_stay_hard_stops_without_any_confirmation(case: str) -> None:
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

    result = await engine.ainvoke(_graph_input(_carrier(fixture["spec"])), _config())

    assert result["stop_reason"] == fixture["code"]
    assert "__interrupt__" not in result
    assert result["pending_decision"] is None
    assert result["decision_status"] is None
    assert result["execution_record"] is None
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0


@pytest.mark.asyncio
async def test_capability_denial_precedes_any_confirmation() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
    )

    result = await engine.ainvoke(_graph_input(_carrier(), effective_mode=None), _config())

    assert result["stop_reason"] == "ad_hoc_calculation_capability_denied"
    assert "__interrupt__" not in result
    assert result["pending_decision"] is None
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
# --- deliverable 2: §4.3 human waiting does not eat the deadline --------------


@pytest.mark.asyncio
async def test_human_wait_does_not_consume_the_calculation_deadline(monkeypatch: Any) -> None:
    clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(engine_module, "_utc_now", clock.now)
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )
    config = _config(deadline_ms=10_000)

    paused = await engine.ainvoke(_graph_input(_carrier()), config)
    assert paused["decision_status"] == "awaiting_decision"
    assert paused["human_wait_started_at"] is not None
    assert paused["human_wait_ms"] == 0

    # The human waits 60s: FAR beyond both the 10s request deadline and every
    # route deadline, so an un-deducted wall-clock deadline could not execute.
    clock.advance(seconds=60)
    assert 60_000 > 10_000

    resumed = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert resumed["human_wait_ms"] == 60_000
    assert resumed["human_wait_started_at"] is None
    assert resumed["human_wait_last_resumed_at"] is not None
    assert resumed["stop_reason"] is None
    assert resumed["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2


@pytest.mark.asyncio
async def test_consumed_budget_is_not_reset_across_the_confirmation(monkeypatch: Any) -> None:
    clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(engine_module, "_utc_now", clock.now)
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )
    config = _config(deadline_ms=10_000)

    paused = await engine.ainvoke(_graph_input(_carrier()), config)
    assert paused["budget_record"]["usage"]["model_calls"] == 0

    # Simulate an allowance the run had ALREADY consumed before suspending: a
    # continuation re-route must carry it forward, never hand back a fresh one.
    seeded_record = json.loads(json.dumps(paused["budget_record"]))
    seeded_record["usage"]["model_calls"] = 1
    await engine.aupdate_state(config, {"budget_record": seeded_record}, as_node="compile")
    clock.advance(seconds=60)

    resumed = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    usage = resumed["budget_record"]["usage"]
    assert usage["model_calls"] == 1  # carried forward, never reset to 0
    assert usage["sql_candidates"] == 2  # the REAL AD_HOC dependency fetches
    assert usage["sql_executions"] == 2
    assert resumed["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1


@pytest.mark.asyncio
async def test_missing_human_wait_record_is_conservative(monkeypatch: Any) -> None:
    clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(engine_module, "_utc_now", clock.now)
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(_plan()),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )
    config = _config(deadline_ms=10_000)

    paused = await engine.ainvoke(_graph_input(_carrier()), config)
    assert paused["human_wait_started_at"] is not None
    # The wait interval is UNDETERMINED (record lost): nothing may be deducted.
    await engine.aupdate_state(config, {"human_wait_started_at": None}, as_node="compile")
    clock.advance(seconds=60)

    resumed = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert resumed["human_wait_ms"] == 0
    assert resumed["human_wait_started_at"] is None
    assert resumed["stop_reason"] == "execution_deadline_unavailable"
    assert resumed["execution_record"]["status"] == "deadline_exceeded"
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0

@pytest.mark.asyncio
async def test_a_genuinely_consumed_model_call_survives_a_long_human_wait(monkeypatch: Any) -> None:
    """A REAL pre-suspension consumption is neither reset nor re-timed."""

    clock = _FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(engine_module, "_utc_now", clock.now)
    engine, runner = _legacy_deep_engine()
    config = _config(revision="rev-bound", deadline_ms=30_000)

    paused = await engine.ainvoke(_graph_input(None, effective_mode=None), config)
    assert paused["route_record"]["route"] == "deep"
    assert paused["needs_hitl"] is True
    # A real model call was CONSUMED before the run suspended for the human.
    assert paused["budget_record"]["usage"]["model_calls"] == 1
    assert runner.execute_calls == 0
    assert paused["human_wait_started_at"] is not None

    # The human waits 120s: four times the 30s request deadline.
    clock.advance(seconds=120)
    assert 120_000 > 30_000

    resumed = await engine.ainvoke(
        Command(
            resume={
                "action": "approve",
                "idempotency_key": "approve-1",
                "expected_version": 1,
            }
        ),
        _config(revision="rev-current", deadline_ms=30_000),
    )

    assert resumed["human_wait_ms"] == 120_000
    assert resumed["hitl_status"] == "approved_continuing"
    assert resumed["stop_reason"] is None
    assert resumed["execution_record"]["status"] == "succeeded"
    assert runner.execute_calls == 1
    # Consumed BEFORE the wait and still consumed AFTER it: never reset.
    assert resumed["budget_record"]["usage"]["model_calls"] == 1
    assert resumed["budget_record"]["usage"]["sql_executions"] == 1

@pytest.mark.asyncio
async def test_clarified_question_is_confirmed_against_the_formula_before_execution() -> None:
    """The TWO-STAGE path is reachable: clarify -> replan -> formula confirm."""

    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    provider = _SlotReplanProvider(
        _plan(unresolved_slots=("time",)),
        _plan(),
    )
    engine, _ = _engine(
        metric_runner=metric_runner,
        provider=provider,  # type: ignore[arg-type]
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
        route_policy=_standard_route_policy(),
    )
    config = _config()

    first = await engine.ainvoke(_graph_input(_carrier()), config)
    assert first["pending_decision"]["decision_kind"] == "clarification"
    assert first["decision_version"] == 1
    assert ad_hoc_runner.calls == 0

    second = await engine.ainvoke(
        Command(
            resume={
                "action": "resolve",
                "slot_bindings": [{"slot": "time", "value": "today"}],
                "idempotency_key": "k-clarify",
            }
        ),
        config,
    )

    # The question is now parsed; the EXPLICIT formula is confirmed against the
    # RESOLVED plan, not the original slot-bearing one.
    assert second["decision_status"] == "awaiting_decision"
    assert second["decision_version"] == 2
    assert second["pending_decision"]["issue_codes"] == [
        "custom_metric_plan_confirmation"
    ]
    assert second["pending_decision"]["plan_sha256"] == (
        second["resolved_plan_validation"]["query_plan_sha256"]
    )
    assert provider.slot_calls == 1
    assert ad_hoc_runner.calls == 0
    assert metric_runner.execute_calls == 0

    third = await engine.ainvoke(Command(resume=_confirm_payload("k-confirm")), config)

    assert third["decision_status"] == "resolved_continuing_current_authorization"
    assert third["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2
    assert "40.0000" in third["grounded_answer_text"]

@pytest.mark.asyncio
async def test_modify_is_no_longer_offered_on_a_formula_confirmation() -> None:
    """P7: `modify` is REMOVED from the formula-plan confirmation.

    The pre-P7 finding was that `modify` could not carry a correction payload at
    all: the frozen business_confirmation action set rejects slot bindings on a
    non-resolve action and the request had no unresolved slots, so `modify` could
    only "record a note and stop".  A correction is now expressed by `resolve`
    ONLY.  This test proves, through the REAL compiled engine, that the payload-
    less "change" action is REFUSED with zero execution and the request stays
    suspended so the user can still confirm/resolve/reject it.
    """

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
    request = revalidate_request(paused["pending_decision"])
    assert "modify" not in request.allowed_actions

    refused = await engine.ainvoke(
        Command(resume={"action": "modify", "idempotency_key": "k-modify"}),
        config,
    )

    # The decision is refused BEFORE it is recorded: the run stays suspended on
    # the SAME request and NOTHING was compiled or executed.
    assert refused["decision_status"] == "awaiting_decision"
    assert refused["pending_decision"]["request_id"] == request.request_id
    assert "__interrupt__" in refused
    assert refused.get("continuation_ready") is not True
    assert refused["execution_record"] is None
    assert "decision_action_not_allowed" in str(
        refused["__interrupt__"][0].value.get("previous_failure")
    )
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0

    # ...and the request is still usable: a real confirm still executes once.
    confirmed = await engine.ainvoke(Command(resume=_confirm_payload()), config)
    assert confirmed["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1


@pytest.mark.asyncio
async def test_confirmation_without_a_request_scoped_runtime_never_executes() -> None:
    """A static (factory-less) deployment fails closed AFTER the confirmation.

    The confirmation is recorded, but CURRENT authorization cannot be re-proven
    without a request-scoped runtime, so nothing is compiled or executed.
    """

    metric_runner = _CountingMetricRunner()
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_gateway(),
        route_policy=_standard_route_policy(),
        context_resolver=_Resolver(_context()),
        query_plan_provider=_StaticPlanProvider(_plan()),
        plan_executor=PlanExecutor(metric_runner=metric_runner),
    )
    config = _config()

    paused = await engine.ainvoke(_graph_input(_carrier()), config)
    assert paused["decision_status"] == "awaiting_decision"
    assert paused["pending_decision"]["issue_codes"] == [
        "custom_metric_plan_confirmation"
    ]

    resumed = await engine.ainvoke(Command(resume=_confirm_payload()), config)

    assert resumed["decision_status"] == "resolved_pending_current_authorization"
    assert resumed["ad_hoc_confirmation_satisfied"] is True
    assert resumed.get("continuation_ready") is not True
    assert resumed["execution_record"] is None
    assert resumed["stop_reason"] is None
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
# --- product surface: the REAL /actions route accepts the new confirmation ----


class _ApiContainer:
    def __init__(self, engine: Any, provider: Any) -> None:
        self._engine = engine
        self._provider = provider

    async def get_engine(self) -> Any:
        return self._engine

    def get_backend_authorization_provider(self) -> Any:
        return self._provider


class _StaticAuthorizationProvider:
    def __init__(self, context: AuthorizationContext) -> None:
        self.context = context

    async def load(self, user: AuthUser) -> AuthorizationContext:
        del user
        return self.context


def _api_client(engine: Any) -> TestClient:
    app = FastAPI()
    app.state.container = _ApiContainer(
        engine, _StaticAuthorizationProvider(_authorization("rev-1"))
    )
    register_v2_routes(app)

    async def analyst() -> AuthUser:
        return AuthUser(
            user_id="analyst",
            telephone=None,
            roles=["analyst"],
            permissions=["*"],
        )

    app.dependency_overrides[require_nl2sql_permission] = analyst
    return TestClient(app)


@pytest.mark.asyncio
async def test_http_confirm_of_a_formula_confirmation_executes_once_and_is_idempotent() -> None:
    """The REAL product route records the confirmation and executes once.

    This is the production path: a real engine plus the real FastAPI route.  A
    typed "confirm" is only recordable because src/nl2sql/v2.py
    _TYPED_RECORDED_STATUS maps it (its absence made every typed confirm answer
    409 "typed decision was not recorded" after the engine had already run).
    """

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
    pending = revalidate_request(paused["pending_decision"])
    assert pending.decision_kind == "metric_plan_confirmation"
    assert pending.issue_codes == ("custom_metric_plan_confirmation",)

    decision = HITLDecision(
        request_id=pending.request_id,
        request_version=pending.version,
        action="confirm",
        idempotency_key="api-confirm",
    )
    payload = {
        "target_type": "typed_decision",
        "decision": decision.model_dump(mode="json"),
        "expected_version": pending.version,
    }
    url = f"/api/v2/nl2sql/threads/{THREAD_ID}/actions"

    with _api_client(engine) as client:
        first = client.post(url, json=payload)
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["status"] == "recorded"
        assert first_body["idempotent"] is False
        assert (
            first_body["continuation_status"]
            == "resolved_continuing_current_authorization"
        )
        assert ad_hoc_runner.calls == 1

        second = client.post(url, json=payload)
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["idempotent"] is True
        assert second_body["status"] == "recorded"

    # The duplicate POST never executed the formula a second time.
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2
    snapshot = await engine.aget_state(config)
    assert snapshot.values["execution_record"]["status"] == "succeeded"

@pytest.mark.asyncio
async def test_http_typed_confirm_executes_once_and_a_retry_is_idempotent() -> None:
    """A retry with the SAME idempotency key must not execute a second time."""

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
    pending = revalidate_request(paused["pending_decision"])
    decision = HITLDecision(
        request_id=pending.request_id,
        request_version=pending.version,
        action="confirm",
        idempotency_key="api-confirm-retry",
    )
    payload = {
        "target_type": "typed_decision",
        "decision": decision.model_dump(mode="json"),
        "expected_version": pending.version,
    }
    url = f"/api/v2/nl2sql/threads/{THREAD_ID}/actions"

    with _api_client(engine) as client:
        first = client.post(url, json=payload)
        assert first.status_code == 200, first.text
        assert first.json()["idempotent"] is False
        assert ad_hoc_runner.calls == 1
        retry = client.post(url, json=payload)
        assert retry.status_code == 200, retry.text
        assert retry.json()["idempotent"] is True

    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2


def test_the_recorded_and_decision_status_maps_stay_in_step() -> None:
    """The engine map and the HTTP recorded-status map are ONE vocabulary.

    The engine decides a typed action's status; the route DERIVES the recorded
    status from its own map.  When the two drift, the route answers 409
    "typed decision was not recorded" AFTER the engine has already executed the
    action (this is exactly how the missing "confirm" entry broke the product
    path).  Comparing the LIVE maps fails immediately when either side gains an
    action the other lacks, or maps one action to a different status.
    """

    recorded = v2_module._TYPED_RECORDED_STATUS  # type: ignore[attr-defined]
    engine_status = engine_module._TYPED_DECISION_STATUS
    assert set(recorded) == set(engine_status), (
        "the HTTP recorded-status map and the engine decision-status map must "
        "cover exactly the same actions"
    )
    assert dict(recorded) == dict(engine_status), (
        "the two maps must agree on every action's recorded status"
    )
    # The action this feature REQUIRES is present and behaves as the engine does.
    assert recorded["confirm"] == engine_status["confirm"] == (
        "resolved_pending_revalidation"
    )
