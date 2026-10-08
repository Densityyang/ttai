from __future__ import annotations

from datetime import date
from types import SimpleNamespace
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
    RoutePolicy,
    TimeRange,
)
from src.nl2sql.infra.llm.gateway import (
    FakeProvider,
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.orchestration.execution import (
    MetricStepResult,
    PlanExecutor,
    PreparedMetricStep,
)
from src.nl2sql.ownership import runtime_config
from src.nl2sql.v2 import register_v2_routes

THREAD_ID = UUID("33333333-3333-3333-3333-333333333333")


def _engine() -> Any:
    provider = FakeProvider(
        {
            "small": ProviderResponse(
                content="reviewable plan",
                model="small",
                usage={"input_tokens": 1, "output_tokens": 1},
                finish_reason="stop",
            )
        }
    )
    gateway = ModelGateway(
        providers={"fake": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default", "test-v1", frozenset({"answer"}), ModelTarget("fake", "small", "small"), None
            ),
            "plan.standard": ModelProfile(
                "plan.standard", "test-v1", frozenset({"plan"}), ModelTarget("fake", "small", "small"), None
            ),
        },
    )
    return create_v2_engine(checkpointer=MemorySaver(), model_gateway=gateway)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "feedback", "expected_status"),
    [
        ("approve", None, "approved"),
        ("modify", "use the monthly grain", "modified"),
        ("reject", None, "rejected"),
        ("cancel", None, "cancelled"),
    ],
)
async def test_hitl_actions_resume_a_checkpoint_without_reexecuting_sql(
    action: str, feedback: str | None, expected_status: str
) -> None:
    engine = _engine()
    config = {"configurable": {"thread_id": f"alice:{action}"}}

    paused = await engine.ainvoke({"messages": [{"role": "user", "content": "pii join revenue"}]}, config)

    assert paused["hitl_status"] == "awaiting_action"
    assert paused["messages"][-1].type == "human"
    resumed = await engine.ainvoke(
        Command(
            resume={
                "action": action,
                "feedback": feedback,
                "idempotency_key": f"{action}-once",
                "expected_version": 1,
            }
        ),
        config,
    )

    assert resumed["hitl_status"] == expected_status
    assert resumed["hitl_version"] == 2
    assert resumed["applied_actions"][f"{action}-once"]["status"] == expected_status
    # A legacy action resume appends exactly ONE terminal assistant message.
    # approve is the only action that CAN continue, but this engine has no typed
    # execution pipeline, so it FAILS CLOSED with an explicit reason and does not
    # execute.  modify/reject/cancel remain terminal with no stop reason and no
    # continuation.  (Continuation itself is covered by the dedicated tests
    # below, which wire a real typed plan pipeline.)
    assert len(resumed["messages"]) == 2
    assert resumed.get("continuation_ready") is not True
    if action == "approve":
        assert resumed["stop_reason"] == "continuation_unavailable"
        assert "No business operation was executed" in resumed["messages"][-1].content
    else:
        assert resumed.get("stop_reason") is None


class _ActionEngine:
    def __init__(self, owner_thread: str) -> None:
        self.owner_thread = owner_thread
        self.calls = 0
        self.values: dict[str, object] = {
            "hitl_status": "awaiting_action",
            "hitl_version": 1,
            "applied_actions": {},
        }

    async def aget_state(self, config: dict[str, object]) -> SimpleNamespace:
        thread_id = config["configurable"]["thread_id"]  # type: ignore[index]
        return SimpleNamespace(values=self.values if thread_id == self.owner_thread else {})

    async def ainvoke(self, command: Command, config: dict[str, object]) -> dict[str, object]:
        del config
        self.calls += 1
        payload = command.resume
        assert isinstance(payload, dict)
        status = {"approve": "approved", "modify": "modified", "reject": "rejected", "cancel": "cancelled"}[payload["action"]]
        self.values = {
            "hitl_status": status,
            "hitl_version": 2,
            "applied_actions": {payload["idempotency_key"]: {"status": status, "version": 2}},
        }
        return self.values


class _ActionContainer:
    def __init__(self, engine: _ActionEngine) -> None:
        self.engine = engine

    async def get_engine(self) -> _ActionEngine:
        return self.engine


def test_action_api_is_idempotent_and_enforces_owner_and_version() -> None:
    owner = f"default:alice:{THREAD_ID}"
    engine = _ActionEngine(owner)
    app = FastAPI()
    app.state.container = _ActionContainer(engine)
    register_v2_routes(app)

    async def alice() -> AuthUser:
        return AuthUser(user_id="alice", telephone=None, roles=["analyst"], permissions=["*"])

    app.dependency_overrides[require_nl2sql_permission] = alice
    payload = {"action": "approve", "idempotency_key": "decision-1", "expected_version": 1}
    with TestClient(app) as client:
        first = client.post(f"/api/v2/nl2sql/threads/{THREAD_ID}/actions", json=payload)
        repeated = client.post(f"/api/v2/nl2sql/threads/{THREAD_ID}/actions", json=payload)
        stale = client.post(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/actions",
            json={**payload, "idempotency_key": "decision-2", "expected_version": 1},
        )

    assert first.status_code == 200
    assert first.json() == {"thread_id": str(THREAD_ID), "status": "approved", "version": 2, "idempotent": False}
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert engine.calls == 1
    assert stale.status_code == 409

    async def bob() -> AuthUser:
        return AuthUser(user_id="bob", telephone=None, roles=["analyst"], permissions=["*"])

    app.dependency_overrides[require_nl2sql_permission] = bob
    with TestClient(app) as client:
        denied = client.post(f"/api/v2/nl2sql/threads/{THREAD_ID}/actions", json=payload)
    assert denied.status_code == 404


# --------------------------------------------------------------------------- #
# Legacy approve continuation: approve MUST continue into governed execution
# --------------------------------------------------------------------------- #

_LEGACY_RELEASE_ID = UUID("55555555-5555-5555-5555-555555555555")
_LEGACY_SNAPSHOT_ID = UUID("66666666-6666-6666-6666-666666666666")
_LEGACY_REQUEST_ID = UUID("77777777-7777-7777-7777-777777777777")
_LEGACY_THREAD = UUID("88888888-8888-8888-8888-888888888888")


def _legacy_identity() -> RequestIdentity:
    return RequestIdentity(
        request_id=_LEGACY_REQUEST_ID,
        user_id="alice",
        roles=frozenset({"analyst"}),
        permissions=frozenset({"metrics:read"}),
    )


def _legacy_authorization(revision: str) -> AuthorizationContext:
    return AuthorizationContext(
        authorization_revision=revision,
        agent_enabled=True,
        scope_level="team",
        allowed_scope_ids=("t1",),
    )


def _legacy_context() -> ContextBundle:
    return ContextBundle(
        semantic_release_id=_LEGACY_RELEASE_ID,
        schema_snapshot_id=_LEGACY_SNAPSHOT_ID,
        domains=("finance",),
        asset_ids=("metric.revenue",),
        approved_relation_ids=("relation.revenue_daily",),
        approved_edge_ids=("edge.a", "edge.b"),
        resolution_status="resolved",
    )


def _legacy_plan() -> QueryPlan:
    return QueryPlan(
        intent="metric",
        domain="finance",
        metric_keys=("metric.revenue",),
        time_range=TimeRange(start=date(2026, 1, 1), end=date(2026, 1, 31)),
        grain="month",
        source_strategy="aggregate_first",
        required_permissions=("metrics:read",),
    )


class _LegacyRunner:
    def __init__(self) -> None:
        self.execute_calls = 0

    async def prepare(
        self, *, step: Any, query_plan: Any, context: Any
    ) -> PreparedMetricStep:
        del step, query_plan, context
        return PreparedMetricStep(
            sql_fingerprint="a" * 64,
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
                sql_fingerprint="a" * 64,
                policy_version="query-gateway.test.v1",
                policy_outcome="allow",
            ),
        )


class _LegacyResolver:
    async def resolve(
        self, *, question: str, identity: RequestIdentity, route_hint: str
    ) -> ContextBundle:
        del question, identity, route_hint
        return _legacy_context()


class _LegacyPlanProvider:
    is_deterministic = True

    async def propose(
        self, *, question: str, context: ContextBundle, identity: RequestIdentity
    ) -> QueryPlan:
        del question, context, identity
        return _legacy_plan()


def _deep_route_policy() -> RoutePolicy:
    # A policy that makes the typed, resolved plan route "deep" so the legacy
    # HITL suspension is actually reachable, exactly as the bootstrap policy
    # makes it reachable for the keyword-driven compatibility path.
    return RoutePolicy(
        version="route.test.deep",
        state="bootstrap",
        fast_max_risk=0,
        fast_min_confidence=1.0,
        fast_max_tables=1,
        standard_max_risk=0,
        standard_min_confidence=1.0,
    )


def _legacy_engine() -> tuple[Any, _LegacyRunner]:
    runner = _LegacyRunner()
    provider = FakeProvider(
        {
            "small": ProviderResponse(
                content="reviewable plan",
                model="small",
                usage={"input_tokens": 1, "output_tokens": 1},
                finish_reason="stop",
            )
        }
    )
    gateway = ModelGateway(
        providers={"fake": provider},
        profiles={
            "fast.default": ModelProfile(
                "fast.default",
                "test-v1",
                frozenset({"answer"}),
                ModelTarget("fake", "small", "small"),
                None,
            ),
            "plan.standard": ModelProfile(
                "plan.standard",
                "test-v1",
                frozenset({"plan"}),
                ModelTarget("fake", "small", "small"),
                None,
            ),
        },
    )
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=gateway,
        route_policy=_deep_route_policy(),
        context_resolver=_LegacyResolver(),
        query_plan_provider=_LegacyPlanProvider(),
        plan_executor=PlanExecutor(metric_runner=runner),
    )
    return engine, runner


def _legacy_config(
    *, authorization: AuthorizationContext | None
) -> dict[str, Any]:
    return runtime_config(
        RequestContext(
            identity=_legacy_identity(),
            thread_id=_LEGACY_THREAD,
            trace_id="trace-legacy-hitl",
            deadline_ms=30_000,
            authorization=authorization,
        )
    )


async def _legacy_suspended(engine: Any, runner: _LegacyRunner) -> dict[str, Any]:
    paused = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "show revenue"}]},
        _legacy_config(authorization=_legacy_authorization("rev-bound")),
    )
    assert paused["route_record"]["route"] == "deep"
    assert paused["hitl_status"] == "awaiting_action"
    assert paused["needs_hitl"] is True
    assert runner.execute_calls == 0
    return dict(paused)


@pytest.mark.asyncio
async def test_legacy_approve_continues_into_governed_execution() -> None:
    """approve -> CURRENT revalidation -> compile -> execute, exactly once."""

    engine, runner = _legacy_engine()
    await _legacy_suspended(engine, runner)

    resumed = await engine.ainvoke(
        Command(
            resume={
                "action": "approve",
                "idempotency_key": "approve-1",
                "expected_version": 1,
            }
        ),
        _legacy_config(authorization=_legacy_authorization("rev-current")),
    )

    assert resumed["hitl_status"] == "approved_continuing"
    assert resumed["continuation_ready"] is True
    assert resumed["stop_reason"] is None
    assert resumed["execution_record"]["status"] == "succeeded"
    assert resumed["grounded_answer_text"]
    assert runner.execute_calls == 1
    # CURRENT authorization replaces the run-bound snapshot for execution.
    assert resumed["authorization_revision"] == "rev-current"
    assert resumed["authorization_context"]["authorization_revision"] == "rev-current"
    # Budget is NOT reset: the deep model call made BEFORE the approval is still
    # counted, and the new SQL execution is added on top.
    assert resumed["budget_record"]["usage"]["model_calls"] == 1
    assert resumed["budget_record"]["usage"]["sql_executions"] == 1


@pytest.mark.asyncio
async def test_legacy_approve_fails_closed_without_current_authorization() -> None:
    engine, runner = _legacy_engine()
    await _legacy_suspended(engine, runner)

    resumed = await engine.ainvoke(
        Command(
            resume={
                "action": "approve",
                "idempotency_key": "approve-1",
                "expected_version": 1,
            }
        ),
        _legacy_config(authorization=None),
    )

    assert resumed["hitl_status"] == "approved"
    assert resumed["stop_reason"] == "current_authorization_unavailable"
    assert resumed.get("continuation_ready") is not True
    assert runner.execute_calls == 0
    assert "Nothing was executed" in resumed["messages"][-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_status"),
    [("modify", "modified"), ("reject", "rejected"), ("cancel", "cancelled")],
)
async def test_legacy_non_approval_actions_never_execute(
    action: str, expected_status: str
) -> None:
    engine, runner = _legacy_engine()
    await _legacy_suspended(engine, runner)
    payload: dict[str, Any] = {
        "action": action,
        "idempotency_key": f"{action}-1",
        "expected_version": 1,
    }
    if action == "modify":
        payload["feedback"] = "use the monthly grain"

    resumed = await engine.ainvoke(
        Command(resume=payload),
        _legacy_config(authorization=_legacy_authorization("rev-current")),
    )

    assert resumed["hitl_status"] == expected_status
    assert resumed.get("continuation_ready") is not True
    assert resumed.get("stop_reason") is None
    assert runner.execute_calls == 0


class _LegacyEngineContainer:
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


@pytest.mark.asyncio
async def test_legacy_approve_api_is_idempotent_and_continues_once() -> None:
    """The /actions guard makes a repeated approve a no-op, never a re-execution."""

    engine, runner = _legacy_engine()
    await _legacy_suspended(engine, runner)

    app = FastAPI()
    app.state.container = _LegacyEngineContainer(
        engine, _StaticAuthorizationProvider(_legacy_authorization("rev-current"))
    )
    register_v2_routes(app)

    async def alice() -> AuthUser:
        return AuthUser(
            user_id="alice", telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = alice
    payload = {"action": "approve", "idempotency_key": "approve-api", "expected_version": 1}
    with TestClient(app) as client:
        first = client.post(
            f"/api/v2/nl2sql/threads/{_LEGACY_THREAD}/actions", json=payload
        )
        repeated = client.post(
            f"/api/v2/nl2sql/threads/{_LEGACY_THREAD}/actions", json=payload
        )

    assert first.status_code == 200
    assert first.json()["status"] == "approved"
    assert first.json()["idempotent"] is False
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True
    assert runner.execute_calls == 1
