"""Typed continuation is REACHABLE: resolve/confirm -> revalidate -> compile -> execute.

These tests drive the REAL graph through a full typed suspension and prove the
continuation actually executes exactly once under CURRENT authorization, rather
than stopping after recording the decision.  A stub request-scoped runtime
factory stands in for the production one; it is rebuilt per invocation exactly
like the real factory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import (
    AuthorizationContext,
    ContextBundle,
    ExecutionReceipt,
    QueryPlan,
    RequestContext,
    RequestIdentity,
    TimeRange,
    evaluate_authorization,
    query_plan_payload,
)
from src.nl2sql.infra.llm.gateway import (
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.decision_contract import (
    HITLDecision,
    HITLRequest,
    SlotBinding,
)
from src.nl2sql.orchestration.engine import (
    _TYPED_DECISION_PAYLOAD_KEYS,
    _carry_forward_budget,
    _typed_decision_from_payload,
    create_v2_engine,
)
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
)
from src.nl2sql.orchestration.planning import PlanValidator
from src.nl2sql.orchestration.typed_runtime import TypedRuntimeUnavailable
from src.nl2sql.ownership import runtime_config
from src.nl2sql.v2 import register_v2_routes

RELEASE_ID = UUID("11111111-1111-1111-1111-111111111111")
SNAPSHOT_ID = UUID("22222222-2222-2222-2222-222222222222")
REQUEST_ID = UUID("33333333-3333-3333-3333-333333333333")
THREAD_ID = UUID("44444444-4444-4444-4444-444444444444")
SQL_FINGERPRINT = "a" * 64
ROUND_TIME = TimeRange(start=date(2026, 9, 20), end=date(2026, 9, 20))


def _identity() -> RequestIdentity:
    return RequestIdentity(
        request_id=REQUEST_ID,
        user_id="analyst",
        roles=frozenset({"analyst"}),
        permissions=frozenset({"metrics:read"}),
    )


def _authorization(revision: str) -> AuthorizationContext:
    return AuthorizationContext(
        authorization_revision=revision,
        agent_enabled=True,
        scope_level="team",
        allowed_scope_ids=("t1",),
    )


def _context() -> ContextBundle:
    return ContextBundle(
        semantic_release_id=RELEASE_ID,
        schema_snapshot_id=SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=("metric.revenue",),
        resolution_status="resolved",
    )


def _time_plan() -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=("metric.revenue",),
        time_range=TimeRange(start=date(2026, 8, 1), end=date(2026, 8, 31)),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=("metrics:read",),
        unresolved_slots=("time",),
    )


def _complete_plan() -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=("metric.revenue",),
        time_range=TimeRange(start=date(2026, 9, 1), end=date(2026, 9, 30)),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=("metrics:read",),
    )


class _MetricRunner:
    def __init__(self) -> None:
        self.execute_calls = 0

    async def prepare(
        self, *, step: Any, query_plan: Any, context: Any
    ) -> PreparedMetricStep:
        del step, query_plan, context
        return PreparedMetricStep(
            sql_fingerprint=SQL_FINGERPRINT,
            join_hops=0,
            payload={"sql": "SELECT synthetic_revenue"},
        )

    async def execute(
        self, prepared: PreparedMetricStep, *, timeout_ms: int
    ) -> MetricStepResult:
        del prepared, timeout_ms
        self.execute_calls += 1
        return MetricStepResult(
            value={"revenue": 4242},
            receipt=ExecutionReceipt(
                datasource="synthetic",
                readonly_role="fixture_reader",
                elapsed_ms=1,
                row_count=1,
                sql_fingerprint=SQL_FINGERPRINT,
                policy_version="query-gateway.test.v1",
                policy_outcome="allow",
            ),
        )


class _Resolver:
    async def resolve(
        self, *, question: str, identity: RequestIdentity, route_hint: str
    ) -> ContextBundle:
        del question, identity, route_hint
        return _context()


class _CumulativePlanProvider:
    is_deterministic = True

    def __init__(self) -> None:
        self.slot_calls = 0

    async def propose(
        self, *, question: str, context: ContextBundle, identity: RequestIdentity
    ) -> QueryPlan:
        del question, context, identity
        return _time_plan()

    async def replan_with_slot_bindings(
        self,
        *,
        question: str,
        context: ContextBundle,
        identity: RequestIdentity,
        base_plan: QueryPlan,
        slot_bindings: tuple[SlotBinding, ...],
    ) -> QueryPlan:
        del question, context, identity
        self.slot_calls += 1
        bound = {binding.slot for binding in slot_bindings}
        unresolved = [slot for slot in base_plan.unresolved_slots if slot not in bound]
        return base_plan.model_copy(
            update={"time_range": ROUND_TIME, "unresolved_slots": tuple(unresolved)}
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
    """A per-request factory double; refuses a denied revision fail-closed."""

    def __init__(self, runner: _MetricRunner, provider: _CumulativePlanProvider) -> None:
        self.runner = runner
        self.provider = provider
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        identity: RequestIdentity,
        authorization: AuthorizationContext | None,
        expected_revision: str | None,
    ) -> Any:
        self.calls.append(
            {
                "revision": getattr(authorization, "authorization_revision", None),
                "expected_revision": expected_revision,
            }
        )
        if authorization is None:
            return TypedRuntimeUnavailable(reason="authorization_context_missing")
        if authorization.authorization_revision == "rev-denied":
            return TypedRuntimeUnavailable(reason="authorization_denied")
        return _StubRuntime(
            context_resolver=_Resolver(),
            query_plan_provider=self.provider,
            plan_executor=PlanExecutor(metric_runner=self.runner),
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


def _config(
    *, revision: str | None, trace_id: str = "trace-continuation"
) -> dict[str, Any]:
    return runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_ID,
            trace_id=trace_id,
            deadline_ms=10_000,
            authorization=_authorization(revision) if revision is not None else None,
        )
    )


def _engine(
    runner: _MetricRunner, provider: _CumulativePlanProvider
) -> tuple[Any, _RuntimeFactory]:
    factory = _RuntimeFactory(runner, provider)
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_gateway(),
        typed_runtime_factory=factory,
    )
    return engine, factory


async def _suspend(engine: Any, runner: _MetricRunner) -> dict[str, Any]:
    paused = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        _config(revision="rev-1"),
    )
    assert paused["decision_status"] == "awaiting_decision"
    assert runner.execute_calls == 0
    return dict(paused)


def _resolve_payload() -> dict[str, object]:
    return {
        "action": "resolve",
        "slot_bindings": [{"slot": "time", "value": "today"}],
        "idempotency_key": "k-resolve",
    }


@pytest.mark.asyncio
async def test_resolved_clarification_continues_into_execution() -> None:
    """resolve -> CURRENT revalidation -> compile -> execute, exactly once."""

    runner = _MetricRunner()
    provider = _CumulativePlanProvider()
    engine, factory = _engine(runner, provider)
    await _suspend(engine, runner)

    resumed = await engine.ainvoke(
        Command(resume=_resolve_payload()), _config(revision="rev-2")
    )

    assert resumed["decision_status"] == "resolved_continuing_current_authorization"
    assert resumed["continuation_ready"] is True
    assert resumed["stop_reason"] is None
    assert resumed["execution_record"]["status"] == "succeeded"
    assert isinstance(resumed["grounded_answer_text"], str)
    assert resumed["grounded_answer_text"]
    assert runner.execute_calls == 1
    assert provider.slot_calls == 1
    # The route decision is REAL and declared, so execute_node could bind to it.
    assert resumed["route_record"]["route"] in {"fast", "standard", "deep"}
    assert resumed["budget_record"]["route"] == resumed["route_record"]["route"]
    # CURRENT authorization (not the run-bound rev-1) is what execution carries.
    assert resumed["authorization_revision"] == "rev-2"
    assert resumed["authorization_context"]["authorization_revision"] == "rev-2"
    # The factory was rebuilt under CURRENT authorization with no run binding.
    assert factory.calls[-1] == {"revision": "rev-2", "expected_revision": None}


@pytest.mark.asyncio
async def test_confirm_continues_into_execution() -> None:
    """Confirm (the typed approve alias) continues a complete plan."""

    runner = _MetricRunner()
    provider = _CumulativePlanProvider()
    engine, _ = _engine(runner, provider)
    await _suspend(engine, runner)
    await _inject_business_confirmation(engine)

    resumed = await engine.ainvoke(
        Command(resume={"action": "confirm", "idempotency_key": "k-confirm"}),
        _config(revision="rev-2"),
    )

    assert resumed["decision_status"] == "resolved_continuing_current_authorization"
    assert resumed["continuation_ready"] is True
    assert resumed["execution_record"]["status"] == "succeeded"
    assert resumed["grounded_answer_text"]
    assert runner.execute_calls == 1
    assert provider.slot_calls == 1


async def _inject_business_confirmation(engine: Any) -> None:
    plan = _complete_plan()
    validator = PlanValidator()
    validation = validator.validate_query_plan(
        plan=plan, context=_context(), identity=_identity()
    )
    request = HITLRequest(
        request_id="confirm-" + "a" * 32,
        decision_kind="business_confirmation",
        version=1,
        allowed_actions=("confirm", "reject", "cancel"),
        plan_sha256=plan.checksum,
        context_checksum=_context().checksum,
        policy_version=validator.policy_version,
        policy_checksum=validator.policy_checksum,
        issue_codes=("custom_metric_plan_confirmation",),
    )
    await engine.aupdate_state(
        _config(revision="rev-1"),
        {
            "pending_decision": request.model_dump(mode="json"),
            "decision_status": "awaiting_decision",
            "decision_version": 1,
            "query_plan": query_plan_payload(plan),
            "query_plan_validation": validation.model_dump(mode="json"),
            "resolved_plan": None,
        },
        as_node="validate",
    )


@pytest.mark.asyncio
async def test_missing_current_authorization_fails_closed_without_execution() -> None:
    runner = _MetricRunner()
    provider = _CumulativePlanProvider()
    engine, _ = _engine(runner, provider)
    await _suspend(engine, runner)

    resumed = await engine.ainvoke(
        Command(resume=_resolve_payload()), _config(revision=None)
    )

    assert resumed["decision_status"] == "current_authorization_unavailable"
    assert resumed["stop_reason"] == "current_authorization_unavailable"
    assert resumed.get("continuation_ready") is not True
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_denied_current_authorization_fails_closed_without_execution() -> None:
    runner = _MetricRunner()
    provider = _CumulativePlanProvider()
    engine, _ = _engine(runner, provider)
    await _suspend(engine, runner)

    resumed = await engine.ainvoke(
        Command(resume=_resolve_payload()), _config(revision="rev-denied")
    )

    assert resumed["decision_status"] == "current_authorization_denied"
    assert resumed["stop_reason"] == "authorization_denied"
    assert resumed.get("continuation_ready") is not True
    assert runner.execute_calls == 0


@pytest.mark.asyncio
async def test_repeated_resume_does_not_execute_twice() -> None:
    runner = _MetricRunner()
    provider = _CumulativePlanProvider()
    engine, factory = _engine(runner, provider)
    await _suspend(engine, runner)

    first = await engine.ainvoke(
        Command(resume=_resolve_payload()), _config(revision="rev-2")
    )
    assert first["execution_record"]["status"] == "succeeded"
    assert runner.execute_calls == 1

    second = await engine.ainvoke(
        Command(resume=_resolve_payload()), _config(revision="rev-2")
    )
    assert second["execution_record"]["status"] == "succeeded"
    assert runner.execute_calls == 1
    # The runtime was rebuilt exactly once for the continuation, never per replay.
    assert factory.calls.count({"revision": "rev-2", "expected_revision": None}) == 1


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


def _api_identity() -> RequestIdentity:
    return RequestIdentity(
        request_id=REQUEST_ID,
        user_id="alice",
        roles=frozenset({"analyst"}),
        permissions=frozenset({"metrics:read"}),
    )


def _api_config() -> dict[str, Any]:
    return runtime_config(
        RequestContext(
            identity=_api_identity(),
            thread_id=THREAD_ID,
            trace_id="trace-api-continuation",
            deadline_ms=10_000,
            authorization=_authorization("rev-1"),
        )
    )


@pytest.mark.asyncio
async def test_http_typed_decision_resume_is_recorded_and_continues() -> None:
    """The REAL /actions HTTP path must record the decision and then execute.

    This is the production path: a real engine plus the real FastAPI route.  It
    guards the deny-by-default resume boundary: the route must send exactly the
    fields the engine accepts, or every production resume fails with
    typed_decision_unknown_field and is never recorded.
    """

    runner = _MetricRunner()
    provider = _CumulativePlanProvider()
    engine, factory = _engine(runner, provider)
    paused = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]}, _api_config()
    )
    assert paused["decision_status"] == "awaiting_decision"
    pending = HITLRequest.model_validate(paused["pending_decision"])
    assert runner.execute_calls == 0

    decision = HITLDecision(
        request_id=pending.request_id,
        request_version=pending.version,
        action="resolve",
        slot_bindings=(SlotBinding(slot="time", value="today"),),
        idempotency_key="api-resolve",
    )
    # REGRESSION GUARD (behaviour, not source text): the payload the route sends
    # is exactly the key set the engine's deny-by-default boundary accepts, and
    # the engine's own parser accepts it against the stored request.
    route_payload = decision.model_dump(mode="json", exclude={"schema_version"})
    assert set(route_payload) - _TYPED_DECISION_PAYLOAD_KEYS == set()
    parsed, failure = _typed_decision_from_payload(pending, route_payload)
    assert failure is None
    assert parsed is not None and parsed.checksum == decision.checksum

    app = FastAPI()
    app.state.container = _ApiContainer(
        engine, _StaticAuthorizationProvider(_authorization("rev-2"))
    )
    register_v2_routes(app)

    async def alice() -> AuthUser:
        return AuthUser(
            user_id="alice", telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = alice
    with TestClient(app) as client:
        response = client.post(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/actions",
            json={
                "target_type": "typed_decision",
                "decision": decision.model_dump(mode="json"),
                "expected_version": pending.version,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "recorded"
    assert body["decision_status"] == "resolved_pending_revalidation"
    assert body["continuation_status"] == "resolved_continuing_current_authorization"

    # The decision was genuinely RECORDED (not a silent 409) and the run
    # continued into governed execution under CURRENT authorization.
    snapshot = await engine.aget_state(_api_config())
    values = snapshot.values
    assert values["decision_ledger"]
    assert values["decision_status"] == "resolved_continuing_current_authorization"
    assert values["continuation_ready"] is True
    assert values["execution_record"]["status"] == "succeeded"
    assert values["grounded_answer_text"]
    assert values["authorization_revision"] == "rev-2"
    assert runner.execute_calls == 1
    assert factory.calls[-1] == {"revision": "rev-2", "expected_revision": None}


def test_carry_forward_budget_preserves_consumed_counters() -> None:
    """A continuation re-route never hands back a fresh, unspent allowance."""

    prior = {
        "route": "fast",
        "usage": {"model_calls": 2, "sql_executions": 1, "join_hops": 3},
        "error_counts": {"policy_denied": 1},
    }
    fresh = {
        "route": "standard",
        "usage": {
            "model_calls": 0,
            "sql_executions": 0,
            "join_hops": 0,
            "repairs": 0,
        },
        "error_counts": {},
    }
    merged = _carry_forward_budget(prior, fresh)
    assert isinstance(merged, dict)
    assert merged["usage"]["model_calls"] == 2
    assert merged["usage"]["sql_executions"] == 1
    assert merged["usage"]["join_hops"] == 3
    assert merged["usage"]["repairs"] == 0
    assert merged["error_counts"] == {"policy_denied": 1}
    # A fresh record is returned unchanged when there is nothing to carry.
    assert _carry_forward_budget(None, fresh) is fresh
