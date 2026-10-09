"""P7: IN-PLACE CORRECTION of an explicit formula at the confirmation step.

These tests drive the REAL compiled LangGraph engine (never a node in isolation)
and prove, with execution-level evidence, that:

* a formula/parse MISALIGNMENT can be corrected IN PLACE: the user binds the
  formula's roles to server-derived, already-authorized candidate metrics and the
  corrected carrier is what actually EXECUTES - exactly once;
* a choice OUTSIDE the authorized candidate set fails closed with a TYPED error
  and ZERO execution, and the refusal never reveals whether the attempted metric
  exists (an authorized-but-not-parsed metric and a nonexistent metric produce the
  IDENTICAL outcome);
* the candidate set is RE-DERIVED server-side at continuation: a TAMPERED
  checkpoint request that advertises a wider candidate set still cannot get an
  unauthorized metric fetched;
* free text and non-identity payloads are refused, and authority/lifecycle/
  canonical/permission fields are refused;
* the frozen kinds (clarification / business_confirmation / risk_policy_decision)
  keep their exact action sets and behaviour;
* confirm without a correction still executes once, and a repeated idempotency
  key never executes twice;
* Mode 3 is untouched: editing a CONFIRMED definition still opens a NEW version
  (A6) and the correction path creates no definition and no canonical authority.

NOTHING here asserts on source text: every claim is produced by running the real
path and observing the result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import ValidationError

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.artifacts.definition_semantics import DefinitionSemantics
from src.nl2sql.artifacts.service import CustomDefinitionService
from src.nl2sql.contracts import (
    AuthorizationContext,
    ContextBundle,
    ExecutionReceipt,
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
from src.nl2sql.orchestration.ad_hoc_request import AdHocCalculationRequest
from src.nl2sql.orchestration.decision_contract import (
    HITLDecision,
    HITLRequest,
    ResolutionOption,
    revalidate_request,
)
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
    RuntimeCalculationRunner,
)
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

# The formula declares revenue/stores; the QUESTION parses into revenue/orders.
# orders is authorized and parsed, stores is authorized but NOT parsed.
_FORMULA_METRICS = ("metric.revenue", "metric.stores")
_PLAN_METRICS = ("metric.revenue", "metric.orders")
_ASSET_IDS = ("metric.revenue", "metric.stores", "metric.orders")
_UNAUTHORIZED = "metric.payroll"
_AUTHORIZED_NOT_PARSED = "metric.stores"


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


def _context(asset_ids: tuple[str, ...] = _ASSET_IDS) -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE_ID,
        schema_snapshot_id=SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=asset_ids,
        resolution_status="resolved",
    )


def _plan(metric_keys: tuple[str, ...] = _PLAN_METRICS) -> QueryPlan:
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
    metric_keys: tuple[str, ...] = _FORMULA_METRICS,
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


def _binding(spec: CalculationSpec) -> CalculationExecutionBinding:
    return CalculationExecutionBinding(
        calculation_id=spec.calculation_id,
        spec_checksum=spec.checksum,
        parameters=(),
    )


def _carrier(spec: CalculationSpec | None = None) -> AdHocCalculationRequest:
    resolved = spec if spec is not None else _ratio_spec()
    return AdHocCalculationRequest(
        calculation_spec=resolved,
        execution_binding=_binding(resolved),
    )


# --- runtime doubles ----------------------------------------------------------


class _CountingMetricRunner:
    """Counts every dependency/direct fetch prepared and executed."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = values or {
            "metric.revenue": 1000,
            "metric.stores": 25,
            "metric.orders": 50,
        }
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

    async def __call__(
        self,
        *,
        identity: RequestIdentity,
        authorization: AuthorizationContext | None,
        expected_revision: str | None,
        capabilities: frozenset[str] = frozenset(),
    ) -> Any:
        del expected_revision
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
        return ProviderResponse(
            content="x", model="small", usage={}, finish_reason="stop"
        )

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


def _config(revision: str = "rev-1") -> dict[str, Any]:
    return runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_ID,
            trace_id="trace-p7-correction",
            deadline_ms=30_000,
            authorization=_authorization(revision),
        )
    )


def _graph_input(carrier: AdHocCalculationRequest) -> dict[str, Any]:
    from src.nl2sql.orchestration.mode_contract import RunEnvelope

    envelope = RunEnvelope(
        run_id="run-p7",
        requested_mode="QUERY",
        effective_mode="QUERY",
    )
    return {
        "messages": [{"role": "user", "content": "revenue per store"}],
        "run_envelope": envelope.model_dump(mode="json"),
        "ad_hoc_calculation": carrier.model_dump(mode="json"),
    }


def _engine(
    *,
    metric_runner: _CountingMetricRunner,
    plan: QueryPlan,
    context: ContextBundle,
    ad_hoc_runner: _CountingAdHocRunner,
) -> Any:
    factory = _RuntimeFactory(
        metric_runner=metric_runner,
        provider=_StaticPlanProvider(plan),
        context=context,
        ad_hoc_runner=ad_hoc_runner,
    )
    return create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_gateway(),
        route_policy=_standard_route_policy(),
        typed_runtime_factory=factory,
    )


def _resolve_payload(
    *bindings: tuple[str, object], key: str = "k-resolve"
) -> dict[str, object]:
    return {
        "action": "resolve",
        "slot_bindings": [
            {"slot": slot, "value": value} for slot, value in bindings
        ],
        "idempotency_key": key,
    }


def _provenance(state: dict[str, Any]) -> dict[str, Any]:
    for block in state.get("response_blocks") or []:
        if isinstance(block, dict) and block.get("type") == "provenance":
            return block
    raise AssertionError("no provenance block was emitted")


def _interrupt_failure(state: dict[str, Any]) -> str:
    interrupts = state.get("__interrupt__") or ()
    assert interrupts, "the run was expected to stay suspended"
    value = interrupts[0].value
    assert isinstance(value, dict)
    return str(value.get("previous_failure"))


async def _suspended_correction_engine() -> tuple[
    Any, _CountingMetricRunner, _CountingAdHocRunner, dict[str, Any]
]:
    """Drive the real engine to the MISALIGNED formula confirmation."""

    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine = _engine(
        metric_runner=metric_runner,
        plan=_plan(),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
    )
    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier()), config)
    assert paused["decision_status"] == "awaiting_decision"
    assert "__interrupt__" in paused
    assert ad_hoc_runner.calls == 0
    return engine, metric_runner, ad_hoc_runner, config


# --- 1. in-place correction succeeds and is what actually executes ------------


@pytest.mark.asyncio
async def test_in_place_correction_executes_exactly_once_with_the_corrected_inputs() -> None:
    engine, metric_runner, ad_hoc_runner, config = (
        await _suspended_correction_engine()
    )

    request = revalidate_request(
        (await engine.aget_state(config)).values["pending_decision"]
    )
    assert request.decision_kind == "metric_plan_confirmation"
    assert request.allowed_actions == ("confirm", "resolve", "reject", "cancel")
    assert "modify" not in request.allowed_actions
    assert request.unresolved_slots == ()
    # The parse disagreed with the formula, and the correction surface names the
    # formula's roles with the SERVER-DERIVED, already-authorized candidates.
    assert request.safe_summary is not None
    assert "| MISMATCH" in request.safe_summary
    assert "numerator=metric.revenue" in request.safe_summary
    assert "denominator=metric.stores" in request.safe_summary
    assert "Question-resolved inputs: metric.revenue, metric.orders" in (
        request.safe_summary
    )
    options = {item.slot: item.candidates for item in request.resolution_options}
    assert options == {
        "numerator": ("metric.revenue", "metric.orders"),
        "denominator": ("metric.revenue", "metric.orders"),
    }
    # The unauthorized metric is NOT named anywhere on the display surface.
    assert _UNAUTHORIZED not in (request.safe_summary or "")

    corrected_spec = _ratio_spec(("metric.revenue", "metric.orders"))
    corrected_id = derived_output_id(corrected_spec, _binding(corrected_spec))

    result = await engine.ainvoke(
        Command(
            resume=_resolve_payload(
                ("numerator", "metric.revenue"),
                ("denominator", "metric.orders"),
            )
        ),
        config,
    )

    # EXACTLY ONCE, and the shared arithmetic ran on the CORRECTED inputs.
    assert result["stop_reason"] is None
    assert result["decision_status"] == "resolved_continuing_current_authorization"
    assert result["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.prepare_calls == 2
    assert metric_runner.execute_calls == 2
    assert ad_hoc_runner.last_inputs == {"numerator": 1000, "denominator": 50}
    # 1000 / 50 = 20.0000 - the CORRECTED derivation, not the declared one.
    assert "20.0000" in result["grounded_answer_text"]
    # The run's carrier IS the corrected one, and the executed identity is the
    # corrected one.
    inputs = result["ad_hoc_calculation"]["calculation_spec"]["inputs"]
    assert [item["metric_key"] for item in inputs] == [
        "metric.revenue",
        "metric.orders",
    ]
    assert result["ad_hoc_calculation"]["execution_binding"]["spec_checksum"] == (
        corrected_spec.checksum
    )
    provenance = _provenance(result)
    assert provenance["derived_output_ids"] == [corrected_id]
    assert provenance["metric_keys"] == []
    assert provenance["calculation_scope"] == "ad_hoc_noncanonical"
    # The correction is audited, and it names only AUTHORIZED metrics.
    corrected_events = [
        event
        for event in result["trace_events"]
        if event.get("name") == "metric_plan_corrected"
    ]
    assert len(corrected_events) == 1
    assert corrected_events[0]["attributes"]["roles"] == [
        "denominator",
        "numerator",
    ]


# --- 2. an OUT-OF-CANDIDATE correction fails closed (MOST IMPORTANT) ----------


@pytest.mark.asyncio
async def test_unauthorized_metric_correction_fails_closed_without_execution() -> None:
    engine, metric_runner, ad_hoc_runner, config = (
        await _suspended_correction_engine()
    )

    refused = await engine.ainvoke(
        Command(
            resume=_resolve_payload(("denominator", _UNAUTHORIZED), key="k-unauth")
        ),
        config,
    )

    # TYPED refusal, ZERO execution, and the run stays suspended on the SAME
    # request so the user can try an authorized choice.
    assert _interrupt_failure(refused) == (
        "typed_decision_rejected:decision_binding_value_not_a_candidate"
    )
    assert refused["decision_status"] == "awaiting_decision"
    assert refused["execution_record"] is None
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
    # NOTHING is leaked about the attempted metric: neither its name nor whether
    # it exists appears on the client-visible surface.
    visible = _interrupt_failure(refused) + "".join(
        str(getattr(message, "content", ""))
        for message in refused.get("messages", [])
    )
    assert _UNAUTHORIZED not in visible


@pytest.mark.asyncio
async def test_the_refusal_is_identical_for_nonexistent_and_authorized_unparsed_metrics() -> None:
    """The candidate SET is the gate - not bare authorization, and not existence.

    'metric.stores' is AUTHORIZED for this run and IS declared by the formula, yet
    it is not a PARSED candidate for the question-resolved plan.  It must be
    refused exactly like a metric that appears nowhere, and neither refusal may
    reveal which case it was.
    """

    engine, metric_runner, ad_hoc_runner, config = (
        await _suspended_correction_engine()
    )
    nonexistent = await engine.ainvoke(
        Command(
            resume=_resolve_payload(("denominator", _UNAUTHORIZED), key="k-nonexistent")
        ),
        config,
    )
    authorized_but_unparsed = await engine.ainvoke(
        Command(
            resume=_resolve_payload(
                ("denominator", _AUTHORIZED_NOT_PARSED), key="k-unparsed"
            )
        ),
        config,
    )

    assert _interrupt_failure(nonexistent) == _interrupt_failure(
        authorized_but_unparsed
    )
    assert nonexistent["decision_status"] == authorized_but_unparsed["decision_status"]
    assert nonexistent["stop_reason"] == authorized_but_unparsed["stop_reason"]
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
    for value in (_UNAUTHORIZED, _AUTHORIZED_NOT_PARSED):
        assert value not in _interrupt_failure(nonexistent)


@pytest.mark.asyncio
async def test_a_tampered_checkpoint_cannot_widen_the_candidate_set() -> None:
    """The candidate set is RE-DERIVED server-side at continuation.

    A checkpoint-restored request that ADVERTISES the unauthorized metric as a
    candidate passes the (tampered) request's own validation and is recorded - and
    the continuation STILL refuses it, because the candidates are recomputed from
    the restored plan/context.  This is the invariant that makes the correction
    payload "a choice inside the existing authorization", never a widening of it.
    """

    engine, metric_runner, ad_hoc_runner, config = (
        await _suspended_correction_engine()
    )
    snapshot = await engine.aget_state(config)
    tampered = json.loads(json.dumps(snapshot.values["pending_decision"]))
    for option in tampered["resolution_options"]:
        option["candidates"] = [*option["candidates"], _UNAUTHORIZED]
    # The tampered request is a CONTRACT-VALID request: its candidates are just
    # strings, so only the server-side re-derivation can catch it.
    assert _UNAUTHORIZED in [
        candidate
        for option in tampered["resolution_options"]
        for candidate in option["candidates"]
    ]
    revalidate_request(tampered)
    await engine.aupdate_state(
        config, {"pending_decision": tampered}, as_node="compile"
    )

    result = await engine.ainvoke(
        Command(
            resume=_resolve_payload(("denominator", _UNAUTHORIZED), key="k-tampered")
        ),
        config,
    )

    # TYPED, terminal fail-closed stop: the ENGINE, not the payload, decides.
    assert result["decision_status"] == "metric_plan_correction_rejected"
    assert result["stop_reason"] == "metric_plan_correction_value_not_a_candidate"
    assert result["execution_record"] is None
    assert result["pending_decision"] is None
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
    assert _UNAUTHORIZED not in str(result["stop_reason"]) + "".join(
        str(getattr(message, "content", ""))
        for message in result.get("messages", [])
    )


@pytest.mark.asyncio
async def test_a_role_outside_the_formula_is_refused_without_execution() -> None:
    engine, metric_runner, ad_hoc_runner, config = (
        await _suspended_correction_engine()
    )

    refused = await engine.ainvoke(
        Command(resume=_resolve_payload(("total", "metric.revenue"), key="k-role")),
        config,
    )

    assert _interrupt_failure(refused) == (
        "typed_decision_rejected:decision_unknown_slot_binding"
    )
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0


# --- 3. free text is refused --------------------------------------------------


@pytest.mark.asyncio
async def test_free_text_and_non_identity_values_are_refused_without_execution() -> None:
    engine, metric_runner, ad_hoc_runner, config = (
        await _suspended_correction_engine()
    )

    free_text = await engine.ainvoke(
        Command(
            resume=_resolve_payload(
                ("denominator", "please just use total sales"), key="k-text"
            )
        ),
        config,
    )
    assert _interrupt_failure(free_text) == (
        "typed_decision_rejected:decision_binding_value_not_a_candidate"
    )
    assert "total sales" not in _interrupt_failure(free_text)

    # A numeric / list payload is never a metric identity either.
    numeric = await engine.ainvoke(
        Command(resume=_resolve_payload(("denominator", 5), key="k-numeric")),
        config,
    )
    assert _interrupt_failure(numeric) == (
        "typed_decision_rejected:decision_binding_value_not_a_candidate"
    )

    assert free_text["decision_status"] == "awaiting_decision"
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0


# --- 4. authority / lifecycle / canonical injection is refused ----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "injected",
    ["authority", "canonical", "canonical_metric_key", "permission", "definition_id",
     "lifecycle", "approved", "saved"],
)
async def test_authority_injection_in_the_payload_is_refused(injected: str) -> None:
    engine, metric_runner, ad_hoc_runner, config = (
        await _suspended_correction_engine()
    )
    payload = _resolve_payload(
        ("numerator", "metric.revenue"), ("denominator", "metric.orders"),
        key="k-authority",
    )
    payload[injected] = True

    refused = await engine.ainvoke(Command(resume=payload), config)

    assert _interrupt_failure(refused) == (
        "typed_decision_authority_field_rejected"
    )
    assert refused["decision_status"] == "awaiting_decision"
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0


@pytest.mark.asyncio
async def test_authority_injection_inside_a_binding_is_refused() -> None:
    engine, metric_runner, ad_hoc_runner, config = (
        await _suspended_correction_engine()
    )
    payload = {
        "action": "resolve",
        "slot_bindings": [
            {"slot": "numerator", "value": "metric.revenue", "canonical": True},
            {"slot": "denominator", "value": "metric.orders"},
        ],
        "idempotency_key": "k-nested-authority",
    }

    refused = await engine.ainvoke(Command(resume=payload), config)

    assert _interrupt_failure(refused) == "typed_decision_payload_invalid"
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0


def test_resolution_options_reject_authority_fields_and_are_kind_scoped() -> None:
    base: dict[str, object] = {
        "request_id": "metric-" + "a" * 32,
        "decision_kind": "metric_plan_confirmation",
        "version": 1,
        "allowed_actions": ("confirm", "resolve", "reject", "cancel"),
        "plan_sha256": "b" * 64,
        "context_checksum": "c" * 64,
        "policy_version": "plan-validation.bootstrap.v1",
        "policy_checksum": "d" * 64,
        "issue_codes": ("custom_metric_plan_confirmation",),
        "resolution_options": (ResolutionOption(slot="r", candidates=("m.one",)),),
    }
    assert HITLRequest(**base).resolution_options[0].candidates == ("m.one",)

    with pytest.raises(ValidationError):
        ResolutionOption(slot="r", candidates=("m.one",), authority="admin")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ResolutionOption(slot="authorization", candidates=("m.one",))
    with pytest.raises(ValidationError):
        ResolutionOption(slot="r", candidates=())
    with pytest.raises(ValidationError):
        ResolutionOption(slot="r", candidates=("m.one", "m.one"))
    with pytest.raises(ValidationError):
        # Duplicate bindable roles.
        HITLRequest(
            **{
                **base,
                "resolution_options": (
                    ResolutionOption(slot="r", candidates=("m.one",)),
                    ResolutionOption(slot="r", candidates=("m.two",)),
                ),
            }
        )
    with pytest.raises(ValidationError):
        # A correction with nothing to choose from would be unbounded.
        HITLRequest(**{**base, "resolution_options": ()})


# --- 5. the FROZEN kinds keep their exact action sets and behaviour -----------


def test_frozen_kinds_keep_their_exact_action_sets() -> None:
    common: dict[str, object] = {
        "request_id": "frozen-" + "a" * 32,
        "version": 1,
        "plan_sha256": "b" * 64,
        "context_checksum": "c" * 64,
        "policy_version": "plan-validation.bootstrap.v1",
        "policy_checksum": "d" * 64,
    }
    # Each frozen kind accepts EXACTLY its frozen action set...
    HITLRequest(
        **{
            **common,
            "decision_kind": "clarification",
            "allowed_actions": ("resolve", "choose", "reject", "cancel"),
            "issue_codes": ("query_plan_unresolved_slots",),
            "unresolved_slots": ("time",),
        }
    )
    HITLRequest(
        **{
            **common,
            "decision_kind": "business_confirmation",
            "allowed_actions": ("confirm", "modify", "reject", "cancel"),
            "issue_codes": ("custom_metric_plan_confirmation",),
        }
    )
    HITLRequest(
        **{
            **common,
            "decision_kind": "risk_policy_decision",
            "allowed_actions": ("confirm", "reject", "cancel"),
            "issue_codes": ("high_risk_execution",),
        }
    )
    # ...and never a widened one.
    for kind, widened in (
        ("clarification", ("resolve", "modify", "reject", "cancel")),
        ("business_confirmation", ("confirm", "resolve", "reject", "cancel")),
        ("risk_policy_decision", ("confirm", "modify", "reject", "cancel")),
    ):
        with pytest.raises(ValidationError):
            HITLRequest(
                **{
                    **common,
                    "decision_kind": kind,
                    "allowed_actions": widened,
                    "issue_codes": ("some_reason",),
                    "unresolved_slots": ("time",) if kind == "clarification" else (),
                }
            )
    # Only the ONE correction kind may carry resolution options.
    for kind, actions, slots in (
        ("clarification", ("resolve", "reject"), ("time",)),
        ("business_confirmation", ("confirm", "reject"), ()),
        ("risk_policy_decision", ("confirm", "reject"), ()),
    ):
        with pytest.raises(ValidationError):
            HITLRequest(
                **{
                    **common,
                    "decision_kind": kind,
                    "allowed_actions": actions,
                    "issue_codes": ("some_reason",),
                    "unresolved_slots": slots,
                    "resolution_options": (
                        ResolutionOption(slot="r", candidates=("m.one",)),
                    ),
                }
            )
    # ...and the correction kind cannot offer modify.
    with pytest.raises(ValidationError):
        HITLRequest(
            **{
                **common,
                "decision_kind": "metric_plan_confirmation",
                "allowed_actions": ("confirm", "modify", "reject", "cancel"),
                "issue_codes": ("custom_metric_plan_confirmation",),
                "resolution_options": (
                    ResolutionOption(slot="r", candidates=("m.one",)),
                ),
            }
        )


def test_frozen_kinds_keep_their_exact_resolution_payload_behaviour() -> None:
    request = HITLRequest(
        request_id="frozen-" + "a" * 32,
        decision_kind="clarification",
        version=1,
        allowed_actions=("resolve", "choose", "reject", "cancel"),
        plan_sha256="b" * 64,
        context_checksum="c" * 64,
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="d" * 64,
        issue_codes=("query_plan_unresolved_slots",),
        unresolved_slots=("time",),
    )
    from src.nl2sql.orchestration.decision_contract import SlotBinding

    bound = HITLDecision(
        request_id=request.request_id,
        request_version=1,
        action="resolve",
        slot_bindings=(SlotBinding(slot="time", value="2026-08"),),
        idempotency_key="idem-frozen",
    )
    assert bound.validate_against(request) == ()
    # The clarification rules are byte-for-byte the frozen ones.
    unknown = HITLDecision(
        request_id=request.request_id,
        request_version=1,
        action="resolve",
        slot_bindings=(SlotBinding(slot="dimension", value="area"),),
        idempotency_key="idem-frozen-2",
    )
    assert unknown.validate_against(request) == ("decision_unknown_slot_binding",)
    # A business_confirmation still refuses a resolution payload outright.
    confirmation = HITLRequest(
        request_id="business-" + "a" * 32,
        decision_kind="business_confirmation",
        version=1,
        allowed_actions=("confirm", "modify", "reject", "cancel"),
        plan_sha256="b" * 64,
        context_checksum="c" * 64,
        policy_version="plan-validation.bootstrap.v1",
        policy_checksum="d" * 64,
        issue_codes=("custom_metric_plan_confirmation",),
    )
    rejected = HITLDecision(
        request_id=confirmation.request_id,
        request_version=1,
        action="confirm",
        slot_bindings=(SlotBinding(slot="time", value="2026-08"),),
        idempotency_key="idem-frozen-3",
    )
    assert rejected.validate_against(confirmation) == (
        "decision_unexpected_resolution_payload",
    )
    assert (
        HITLDecision(
            request_id=confirmation.request_id,
            request_version=1,
            action="confirm",
            idempotency_key="idem-frozen-4",
        ).validate_against(confirmation)
        == ()
    )


# --- 6. idempotency -----------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_correction_with_the_same_key_never_executes_twice() -> None:
    engine, metric_runner, ad_hoc_runner, config = (
        await _suspended_correction_engine()
    )
    payload = _resolve_payload(
        ("numerator", "metric.revenue"),
        ("denominator", "metric.orders"),
        key="k-idempotent",
    )

    first = await engine.ainvoke(Command(resume=payload), config)
    assert first["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2

    second = await engine.ainvoke(Command(resume=payload), config)
    assert second["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2


# --- 7. confirm without a correction still works ------------------------------


@pytest.mark.asyncio
async def test_confirm_without_a_correction_still_executes_once() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine = _engine(
        metric_runner=metric_runner,
        plan=_plan(("metric.revenue", "metric.stores")),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
    )
    config = _config()

    paused = await engine.ainvoke(_graph_input(_carrier()), config)
    assert paused["decision_status"] == "awaiting_decision"
    request = revalidate_request(paused["pending_decision"])
    assert request.decision_kind == "metric_plan_confirmation"
    assert request.allowed_actions == ("confirm", "resolve", "reject", "cancel")
    assert request.safe_summary is not None and request.safe_summary.endswith(
        "| aligned"
    )
    assert ad_hoc_runner.calls == 0

    confirmed = await engine.ainvoke(
        Command(resume={"action": "confirm", "idempotency_key": "k-confirm"}),
        config,
    )

    assert confirmed["stop_reason"] is None
    assert confirmed["execution_record"]["status"] == "succeeded"
    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2
    assert "40.0000" in confirmed["grounded_answer_text"]


# --- 8. Mode 3 (definition lifecycle, A6) is untouched ------------------------


@pytest.mark.asyncio
async def test_editing_a_confirmed_definition_still_opens_a_new_version() -> None:
    """A6 at the DefinitionVersion boundary, re-driven here.

    The in-place correction applies ONLY to the run-scoped, noncanonical AD_HOC
    carrier.  A CONFIRMED definition can never be edited in place: a substantive
    semantic change opens the NEXT draft version and the old confirmed version
    stays addressable exactly as before.
    """

    service = CustomDefinitionService(governed_metric_keys={"demo.revenue"})
    spec = CalculationSpec(
        calculation_id="calc.p7.mode3",
        expression=InputRefOperand(role="r"),
        inputs=(
            CalculationInputSpec(
                role="r", provenance="published_gold", metric_key="demo.revenue"
            ),
        ),
        unit="count",
    )
    draft = await service.create_draft(
        owner_user_id="alice",
        title="Mode 3 metric",
        calculation=spec,
        semantics=DefinitionSemantics(population="all orders"),
    )
    await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    confirmed = await service.confirm(
        owner_user_id="alice", definition_id=draft.definition_id
    )
    assert confirmed.current_version.version == 1
    assert confirmed.current_version.semantic_closed is True

    # (a) A CONFIRMED definition is immutable: there is NO in-place edit path.
    with pytest.raises(ValueError, match="confirmed definition requires a new version"):
        await service.update_draft_with_semantics(
            owner_user_id="alice",
            definition_id=draft.definition_id,
            semantics=DefinitionSemantics(population="paid orders"),
        )
    exact_v1 = await service.get_exact_version(
        owner_user_id="alice", definition_id=draft.definition_id, version=1
    )
    assert exact_v1.semantics == DefinitionSemantics(population="all orders")
    assert exact_v1.semantic_closed is True

    # (b) The A6 version boundary is intact: on a CLOSED (not yet confirmed)
    # version a substantive semantic change OPENS the next version.
    second = await service.create_draft(
        owner_user_id="alice",
        title="Mode 3 metric v2",
        calculation=spec,
        semantics=DefinitionSemantics(population="all orders"),
    )
    await service.mark_semantic_closed(
        owner_user_id="alice", definition_id=second.definition_id
    )
    outcome = await service.update_draft_with_semantics(
        owner_user_id="alice",
        definition_id=second.definition_id,
        semantics=DefinitionSemantics(population="paid orders"),
    )
    assert outcome.material is True
    assert outcome.version_created is True
    assert outcome.requires_business_decision is True
    assert outcome.version.version == 2
    assert outcome.version.semantic_closed is False
    # The superseded v1 was never confirmed as an exact version.
    from src.nl2sql.artifacts.service import DefinitionNotFound

    with pytest.raises(DefinitionNotFound):
        await service.get_exact_version(
            owner_user_id="alice", definition_id=second.definition_id, version=1
        )


def test_the_correction_contract_carries_no_definition_or_lifecycle_surface() -> None:
    """The correction payload has no field a definition edit could ride on."""

    payload = {
        "request_id": "metric-" + "a" * 32,
        "request_version": 1,
        "action": "resolve",
        "slot_bindings": ({"slot": "r", "value": "m.one"},),
        "idempotency_key": "idem-p7",
    }
    from src.nl2sql.orchestration.decision_contract import revalidate_decision

    decision = revalidate_decision(payload)
    dumped = json.dumps(decision.model_dump(mode="json"))
    for forbidden in (
        "definition_id",
        "definition_version",
        "canonical",
        "lifecycle",
        "authority",
        "permission",
    ):
        assert forbidden not in dumped
    for injected in ("definition_id", "definition_version", "canonical_metric_key"):
        with pytest.raises(ValidationError):
            revalidate_decision({**payload, injected: "x"})

# --- 9. the REAL product surface: /actions accepts the correction ------------


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


def _http_decision(engine_request: HITLRequest, **overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "request_id": engine_request.request_id,
        "request_version": engine_request.version,
        "action": "resolve",
        "slot_bindings": [
            {"slot": "numerator", "value": "metric.revenue"},
            {"slot": "denominator", "value": "metric.orders"},
        ],
        "idempotency_key": "api-resolve",
    }
    base.update(overrides)
    return {
        "target_type": "typed_decision",
        "decision": HITLDecision(**base).model_dump(mode="json"),
        "expected_version": engine_request.version,
    }


@pytest.mark.asyncio
async def test_http_resolve_of_a_formula_confirmation_corrects_and_executes_once() -> None:
    """The REAL product route carries the correction end to end.

    This is the reachability proof: a real engine plus the real FastAPI route,
    with a typed "resolve" that carries the corrected role choices.
    """

    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine = _engine(
        metric_runner=metric_runner,
        plan=_plan(),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
    )
    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier()), config)
    pending = revalidate_request(paused["pending_decision"])
    payload = _http_decision(pending)
    url = f"/api/v2/nl2sql/threads/{THREAD_ID}/actions"

    with _api_client(engine) as client:
        first = client.post(url, json=payload)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["status"] == "recorded"
        assert body["idempotent"] is False
        assert body["decision_status"] == "resolved_pending_revalidation"
        assert body["continuation_status"] == (
            "resolved_continuing_current_authorization"
        )
        assert ad_hoc_runner.calls == 1
        assert ad_hoc_runner.last_inputs == {"numerator": 1000, "denominator": 50}

        # The SAME key replays without a second execution.
        second = client.post(url, json=payload)
        assert second.status_code == 200, second.text
        assert second.json()["idempotent"] is True

    assert ad_hoc_runner.calls == 1
    assert metric_runner.execute_calls == 2
    snapshot = await engine.aget_state(config)
    assert snapshot.values["execution_record"]["status"] == "succeeded"
    assert [
        item["metric_key"]
        for item in snapshot.values["ad_hoc_calculation"]["calculation_spec"]["inputs"]
    ] == ["metric.revenue", "metric.orders"]


@pytest.mark.asyncio
async def test_http_out_of_candidate_correction_is_refused_without_execution() -> None:
    metric_runner = _CountingMetricRunner()
    ad_hoc_runner = _CountingAdHocRunner()
    engine = _engine(
        metric_runner=metric_runner,
        plan=_plan(),
        context=_context(),
        ad_hoc_runner=ad_hoc_runner,
    )
    config = _config()
    paused = await engine.ainvoke(_graph_input(_carrier()), config)
    pending = revalidate_request(paused["pending_decision"])
    payload = _http_decision(
        pending,
        slot_bindings=[
            {"slot": "numerator", "value": "metric.revenue"},
            {"slot": "denominator", "value": _UNAUTHORIZED},
        ],
        idempotency_key="api-unauthorized",
    )
    url = f"/api/v2/nl2sql/threads/{THREAD_ID}/actions"

    with _api_client(engine) as client:
        response = client.post(url, json=payload)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "typed decision is stale or conflicting"
    assert _UNAUTHORIZED not in response.text
    assert ad_hoc_runner.calls == 0
    assert metric_runner.prepare_calls == 0
    assert metric_runner.execute_calls == 0
