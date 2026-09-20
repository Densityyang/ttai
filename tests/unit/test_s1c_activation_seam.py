"""P4-S1c activation seam: request-scoped factory, guard, revision and receipt.

These tests exercise the S1c engine seam directly.  They deliberately build
the engine with a typed-runtime FACTORY (never static request-scoped
collaborators) so the factory output is constructed per request, and they prove
the S1c closures A1 (identity binding), A4 (expected_revision) and A5 (receipt
provenance) at the seam that the engine actually calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import src.nl2sql.container as container_module
from src.core.settings import Settings
from src.nl2sql.container import AppContainer
from src.nl2sql.contracts import (
    AuthorizationContext,
    ContextBundle,
    RequestContext,
    RequestIdentity,
)
from src.nl2sql.infra.governance.query_gateway import QueryGateway, QueryReceipt
from src.nl2sql.infra.llm.gateway import (
    FakeProvider,
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.engine import (
    _REQUEST_TYPED_SCOPE,
    TypedRuntimeFactory,
    _signals,
    authorization_run_binding_failure,
    create_v2_engine,
)
from src.nl2sql.orchestration.typed_runtime import (
    RequestTypedRuntime,
    TypedRequestScopeError,
    TypedRuntimeUnavailable,
    build_request_typed_runtime,
)
from src.nl2sql.ownership import runtime_config
from tests.unit.test_typed_runtime import (
    QUESTION,
    RELEASE_ID,
    RELEASE_ID_B,
    SNAPSHOT_ID,
    _active_metrics,
    _Deployment,
    _gateway,
    _identity,
    _relation,
    _release,
    _snapshot,
    _views,
)

THREAD_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
THREAD_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
METRIC_ASSET = "metric.complaint_in_transit_count"


def _authorization(revision: str) -> AuthorizationContext:
    return AuthorizationContext(
        authorization_revision=revision,
        agent_enabled=True,
        scope_level="city_company",
        allowed_scope_ids=("1",),
    )


def _other_identity() -> RequestIdentity:
    return _identity().model_copy(
        update={"request_id": UUID(RELEASE_ID_B), "user_id": "other-reader"}
    )


def _context_bundle() -> ContextBundle:
    return ContextBundle(
        semantic_release_id=UUID(RELEASE_ID),
        schema_snapshot_id=UUID(SNAPSHOT_ID),
        domains=("complaint",),
        asset_ids=(METRIC_ASSET,),
        approved_relation_ids=("relation.synthetic",),
        resolution_status="resolved",
    )


class _CountingProvider:
    provider_name = "fake"

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
        del model, messages, timeout_ms, max_output_tokens, response_schema
        self.calls += 1
        raise AssertionError("the typed fast path must never call the model")

    async def list_models(self) -> tuple[str, ...]:
        return ("small",)


def _model_gateway_with_provider() -> tuple[ModelGateway, _CountingProvider]:
    provider = _CountingProvider()
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
    return gateway, provider


def _model_gateway() -> ModelGateway:
    gateway, _ = _model_gateway_with_provider()
    return gateway


def _deep_model_gateway() -> ModelGateway:
    """A real deep/HITL model path so suspension can be driven end to end."""

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
    return ModelGateway(
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


def _gateway_with_rows(
    rows: list[dict[str, Any]] | None = None,
) -> tuple[QueryGateway, AsyncMock]:
    resolved = rows if rows is not None else [{"value": 7}]
    gateway = QueryGateway(AsyncMock(), schema="ai_views")

    async def _execute(sql: str, params: dict[str, Any]) -> QueryReceipt:
        del params
        prepared = gateway.prepare(sql)
        return QueryReceipt(
            accepted=True,
            sql=sql,
            sql_fingerprint=prepared.fingerprint,
            rows=resolved,
            row_count=len(resolved),
            policy_outcome="allow",
            max_rows=200,
        )

    execute = AsyncMock(side_effect=_execute)
    gateway.execute = execute  # type: ignore[method-assign]
    return gateway, execute


class _RuntimeSpy:
    """An app-scoped factory double: records every per-request invocation."""

    def __init__(
        self,
        deployment: _Deployment,
        gateway: QueryGateway,
        *,
        rotate_to: Any | None = None,
    ) -> None:
        self.deployment = deployment
        self.gateway = gateway
        self.rotate_to = rotate_to
        self.calls: list[dict[str, Any]] = []
        self.outputs: list[RequestTypedRuntime | TypedRuntimeUnavailable] = []

    async def __call__(
        self,
        *,
        identity: RequestIdentity,
        authorization: AuthorizationContext | None,
        expected_revision: str | None,
    ) -> RequestTypedRuntime | TypedRuntimeUnavailable:
        self.calls.append(
            {
                "identity": identity,
                "authorization": authorization,
                "expected_revision": expected_revision,
            }
        )
        output = await build_request_typed_runtime(
            views=_views(),
            read_active=self.deployment.read_active,
            read_snapshot=self.deployment.read_snapshot,
            gateway=self.gateway,
            identity=identity,
            authorization=authorization,
            expected_revision=expected_revision,
        )
        self.outputs.append(output)
        if self.rotate_to is not None:
            self.deployment.release = self.rotate_to
            self.rotate_to = None
        return output


def _engine(factory: TypedRuntimeFactory) -> Any:
    return create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_model_gateway(),
        typed_runtime_factory=factory,
    )


async def _run(
    engine: Any,
    *,
    identity: RequestIdentity,
    authorization: AuthorizationContext | None,
    thread_id: UUID | None = None,
) -> dict[str, Any]:
    context = RequestContext(
        identity=identity,
        thread_id=thread_id or uuid4(),
        trace_id="trace-s1c",
        deadline_ms=4_000,
        authorization=authorization,
    )
    result = await engine.ainvoke(
        {"messages": [{"role": "user", "content": QUESTION}]},
        runtime_config(context),
    )
    return dict(result)


# --------------------------------------------------------------------------- #
# Reachability + canonical fail-closed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_s1c_typed_runtime_is_reachable_and_binds_provenance() -> None:
    deployment = _Deployment(_release(_active_metrics()), _snapshot(_relation()))
    gateway, execute = _gateway_with_rows()
    spy = _RuntimeSpy(deployment, gateway)
    engine = _engine(spy)

    result = await _run(
        engine,
        identity=_identity(),
        authorization=_authorization("rev-A"),
        thread_id=THREAD_A,
    )

    assert result["stop_reason"] is None
    assert result["execution_record"]["status"] == "succeeded"
    assert result["authorization_revision"] == "rev-A"
    assert result["budget_record"]["usage"]["model_calls"] == 0
    assert result["budget_record"]["usage"]["sql_executions"] == 1
    execute.assert_awaited_once()
    assert len(spy.calls) == 1
    assert spy.calls[0]["identity"] == _identity()
    # The factory receives the revision BOUND TO THIS RUN (run consistency).
    assert spy.calls[0]["expected_revision"] == "rev-A"
    assert isinstance(spy.outputs[0], RequestTypedRuntime)
    # The memo cell is gone once the invocation ends: nothing request-scoped is
    # retained at graph/AppContainer lifetime.
    assert _REQUEST_TYPED_SCOPE.get() is None


@pytest.mark.asyncio
async def test_s1c_missing_authorization_fails_closed_canonically() -> None:
    deployment = _Deployment(_release(_active_metrics()), _snapshot(_relation()))
    gateway, execute = _gateway_with_rows()
    spy = _RuntimeSpy(deployment, gateway)
    engine = _engine(spy)

    result = await _run(
        engine,
        identity=_identity(),
        authorization=None,
        thread_id=THREAD_A,
    )

    assert result["stop_reason"] == "typed_runtime_unavailable"
    assert result["typed_runtime_unavailable_reason"] == "authorization_context_missing"
    assert "TypedRuntimeUnavailable" in result["degradation_flags"]
    assert result["execution_record"] is None
    execute.assert_not_awaited()
    # Fail closed BEFORE any release read; no no-auth compiler execution occurs.
    assert deployment.active_calls == 0


# --------------------------------------------------------------------------- #
# A1: identity is BOUND into the runtime and enforced in code
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_s1c_foreign_identity_is_refused_by_the_request_runtime() -> None:
    deployment = _Deployment(_release(_active_metrics()), _snapshot(_relation()))
    runtime = await build_request_typed_runtime(
        views=_views(),
        read_active=deployment.read_active,
        read_snapshot=deployment.read_snapshot,
        gateway=_gateway(),
        identity=_identity(),
        authorization=_authorization("rev-A"),
    )
    assert isinstance(runtime, RequestTypedRuntime)
    assert runtime.identity == _identity()
    assert runtime.authorization_revision == "rev-A"

    with pytest.raises(TypedRequestScopeError, match="request_identity_mismatch"):
        await runtime.context_resolver.resolve(
            question=QUESTION,
            identity=_other_identity(),
            route_hint="standard",
        )
    with pytest.raises(TypedRequestScopeError, match="request_identity_mismatch"):
        await runtime.query_plan_provider.propose(
            question=QUESTION,
            context=_context_bundle(),
            identity=_other_identity(),
        )
    runtime.require_identity(_identity())
    with pytest.raises(TypedRequestScopeError, match="request_identity_mismatch"):
        runtime.require_identity(_other_identity())
    # The owner's own call still succeeds.
    context = await runtime.context_resolver.resolve(
        question=QUESTION,
        identity=_identity(),
        route_hint="standard",
    )
    assert str(context.semantic_release_id) == RELEASE_ID


# --------------------------------------------------------------------------- #
# App-scoped engine traps: A/B isolation + release rotation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_s1c_request_b_does_not_inherit_request_a_components() -> None:
    deployment = _Deployment(_release(_active_metrics()), _snapshot(_relation()))
    gateway, execute = _gateway_with_rows()
    spy = _RuntimeSpy(deployment, gateway)
    engine = _engine(spy)

    first = await _run(
        engine,
        identity=_identity(),
        authorization=_authorization("rev-A"),
        thread_id=THREAD_A,
    )
    second = await _run(
        engine,
        identity=_other_identity(),
        authorization=_authorization("rev-B"),
        thread_id=THREAD_B,
    )

    assert first["authorization_revision"] == "rev-A"
    assert second["authorization_revision"] == "rev-B"
    assert first["context_bundle"]["semantic_release_id"] == RELEASE_ID
    assert second["context_bundle"]["semantic_release_id"] == RELEASE_ID
    runtime_a = spy.outputs[0]
    runtime_b = spy.outputs[1]
    assert isinstance(runtime_a, RequestTypedRuntime)
    assert isinstance(runtime_b, RequestTypedRuntime)
    assert runtime_a is not runtime_b
    assert runtime_a.metric_query_compiler is not runtime_b.metric_query_compiler
    assert runtime_a.active_release_registry is not runtime_b.active_release_registry
    assert runtime_a.plan_executor is not runtime_b.plan_executor
    assert runtime_a.identity != runtime_b.identity
    assert runtime_a.authorization_revision != runtime_b.authorization_revision
    assert [call["identity"] for call in spy.calls] == [_identity(), _other_identity()]
    assert [call["authorization"].authorization_revision for call in spy.calls] == [
        "rev-A",
        "rev-B",
    ]
    assert [call["expected_revision"] for call in spy.calls] == ["rev-A", "rev-B"]
    assert execute.await_count == 2


@pytest.mark.asyncio
async def test_s1c_release_rotation_is_allowed_between_requests_and_pinned_within() -> None:
    release_a = _release(_active_metrics())
    release_b = _release(_active_metrics(), release_id=RELEASE_ID_B)
    deployment = _Deployment(release_a, _snapshot(_relation()))
    gateway, _ = _gateway_with_rows()
    # Rotate the deployment pointer immediately AFTER request 1 pins its runtime;
    # the request must still see release A, while request 2 may see release B.
    spy = _RuntimeSpy(deployment, gateway, rotate_to=release_b)
    engine = _engine(spy)

    first = await _run(
        engine,
        identity=_identity(),
        authorization=_authorization("rev-A"),
        thread_id=THREAD_A,
    )
    assert first["context_bundle"]["semantic_release_id"] == RELEASE_ID
    assert first["execution_record"]["status"] == "succeeded"

    second = await _run(
        engine,
        identity=_identity(),
        authorization=_authorization("rev-A"),
        thread_id=THREAD_B,
    )
    assert second["context_bundle"]["semantic_release_id"] == RELEASE_ID_B
    runtime_a = spy.outputs[0]
    runtime_b = spy.outputs[1]
    assert isinstance(runtime_a, RequestTypedRuntime)
    assert isinstance(runtime_b, RequestTypedRuntime)
    assert runtime_a.release is release_a
    assert runtime_b.release is release_b


# --------------------------------------------------------------------------- #
# A4: expected_revision is RUN-BINDING consistency, not live revalidation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_s1c_factory_run_binding_mismatch_fails_closed() -> None:
    deployment = _Deployment(_release(_active_metrics()), _snapshot(_relation()))
    gateway = _gateway()
    mismatched = await build_request_typed_runtime(
        views=_views(),
        read_active=deployment.read_active,
        read_snapshot=deployment.read_snapshot,
        gateway=gateway,
        identity=_identity(),
        authorization=_authorization("rev-2"),
        expected_revision="rev-1",
    )
    assert isinstance(mismatched, TypedRuntimeUnavailable)
    assert mismatched.reason == "authorization_run_binding_mismatch"

    matched = await build_request_typed_runtime(
        views=_views(),
        read_active=deployment.read_active,
        read_snapshot=deployment.read_snapshot,
        gateway=gateway,
        identity=_identity(),
        authorization=_authorization("rev-1"),
        expected_revision="rev-1",
    )
    assert isinstance(matched, RequestTypedRuntime)
    assert matched.expected_revision == "rev-1"


def test_s1c_run_binding_restores_snapshot_and_ignores_current_config() -> None:
    # No authority bound to this run -> the authorization-free path is unchanged.
    assert authorization_run_binding_failure({}, None) is None

    bound = _authorization("rev-1")
    state = {
        "authorization_context": bound.model_dump(mode="json"),
        "authorization_revision": "rev-1",
    }
    # A resume may carry a DIFFERENT current configurable (a permission change
    # mid-run).  V1 intentionally does NOT apply it: with no explicitly supplied
    # snapshot the run-bound snapshot is RESTORED and the current configurable is
    # never used as a comparison source.
    changed_configurable = runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_A,
            trace_id="trace-s1c",
            authorization=_authorization("rev-2"),
        )
    )["configurable"]
    assert changed_configurable.get("authorization_context") is not None
    assert authorization_run_binding_failure(state, None) is None

    # Re-supplying the SAME run-bound snapshot is internally consistent.
    assert authorization_run_binding_failure(state, bound) is None
    # A DIFFERENT supplied snapshot is a RUN-BINDING mismatch, not a revocation.
    assert (
        authorization_run_binding_failure(state, _authorization("rev-2"))
        == "authorization_run_binding_mismatch"
    )


@pytest.mark.asyncio
async def test_s1c_engine_reads_authorization_exactly_once_at_run_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # D1 structural pin: the engine must read the trusted carrier for authority
    # exactly ONCE, at run start.  Before the fix model_node re-read it at
    # suspension, so this counted 2.
    import src.nl2sql.orchestration.engine as engine_module

    real = engine_module.authorization_context_from_config
    seen: list[int] = []

    def _spy(configurable: Any) -> Any:
        seen.append(1)
        return real(configurable)

    monkeypatch.setattr(engine_module, "authorization_context_from_config", _spy)
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_deep_model_gateway(),
    )
    result = await _run(
        engine,
        identity=_identity(),
        authorization=_authorization("rev-1"),
        thread_id=THREAD_A,
    )

    assert result["authorization_revision"] == "rev-1"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_s1c_authorized_suspend_resume_keeps_the_run_bound_snapshot() -> None:
    # D3 end-to-end: drive the REAL graph through an authorized deep suspension,
    # change the current configurable revision between suspension and resume, and
    # prove the run keeps its ORIGINAL bound snapshot and does not rebind.
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=_deep_model_gateway(),
    )
    suspended_config = runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_A,
            trace_id="trace-s1c",
            deadline_ms=10_000,
            authorization=_authorization("rev-1"),
        )
    )
    paused = await engine.ainvoke(
        {"messages": [{"role": "user", "content": "pii join revenue"}]},
        suspended_config,
    )
    assert paused["hitl_status"] == "awaiting_action"
    assert paused["authorization_revision"] == "rev-1"
    assert paused["authorization_context"]["authorization_revision"] == "rev-1"

    # The current request now carries a DIFFERENT revision.  Frozen V1 must keep
    # the run-bound snapshot and must NOT rebind or fail closed.
    resumed_config = runtime_config(
        RequestContext(
            identity=_identity(),
            thread_id=THREAD_A,
            trace_id="trace-s1c",
            deadline_ms=10_000,
            authorization=_authorization("rev-2"),
        )
    )
    resumed = await engine.ainvoke(
        Command(
            resume={
                "action": "approve",
                "idempotency_key": "once",
                "expected_version": 1,
            }
        ),
        resumed_config,
    )
    assert resumed["hitl_status"] == "approved"
    assert resumed["authorization_revision"] == "rev-1"
    assert resumed["authorization_context"]["authorization_revision"] == "rev-1"


# --------------------------------------------------------------------------- #
# R8 deployment activation posture: default dormant / activated fail-closed
# --------------------------------------------------------------------------- #


def test_s1c_default_deployment_does_not_configure_the_typed_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Test A (structural): the default deployment builds the engine with NO
    # factory, so the existing v2 product path is preserved.
    settings = Settings(_env_file=None, typed_runtime_activation="disabled")
    monkeypatch.setattr(container_module, "get_settings", lambda: settings)
    container = AppContainer()

    assert settings.typed_runtime_enabled is False
    assert container._configured_typed_runtime_factory() is None


@pytest.mark.asyncio
async def test_s1c_activated_deployment_factory_builds_a_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Test B (behavioural): the activated deployment's factory must actually
    # BUILD a usable request-scoped runtime, not merely be callable.
    settings = Settings(
        _env_file=None,
        typed_runtime_activation="trusted_backend_authorization",
    )
    monkeypatch.setattr(container_module, "get_settings", lambda: settings)
    deployment = _Deployment(_release(_active_metrics()), _snapshot(_relation()))
    gateway = _gateway()
    container = AppContainer()

    async def _inputs() -> tuple[Any, Any, Any, Any]:
        return (_views(), deployment.read_active, deployment.read_snapshot, gateway)

    monkeypatch.setattr(container, "_typed_deployment_inputs", _inputs)
    factory = container._configured_typed_runtime_factory()
    assert settings.typed_runtime_enabled is True
    assert factory is not None

    runtime = await factory(
        identity=_identity(),
        authorization=_authorization("rev-A"),
        expected_revision=None,
    )
    assert isinstance(runtime, RequestTypedRuntime)
    assert runtime.identity == _identity()
    assert runtime.authorization_revision == "rev-A"


@pytest.mark.asyncio
async def test_s1c_default_deployment_keeps_v2_capability_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Test A (operational regression guard): drive the REAL default container,
    # prove the compiled graph has NO typed_runtime node, and prove a default
    # request still reaches the model.
    from types import SimpleNamespace

    settings = Settings(_env_file=None, typed_runtime_activation="disabled")
    monkeypatch.setattr(container_module, "get_settings", lambda: settings)
    monkeypatch.setattr(container_module, "build_model_gateway", _deep_model_gateway)
    container = AppContainer()
    container._checkpointer_manager = SimpleNamespace(checkpointer=MemorySaver())
    container._checkpoint_available = True

    engine = await container.get_engine()
    nodes = set(engine.get_graph().nodes)
    assert "typed_runtime" not in nodes
    assert {
        "receive",
        "context",
        "plan",
        "validate",
        "route",
        "compile",
        "execute",
        "model",
        "hitl",
    } <= nodes

    result = await _run(
        engine,
        identity=_identity(),
        authorization=None,
        thread_id=THREAD_A,
    )

    assert result["model_receipt"] is not None
    assert result["stop_reason"] is None
    assert result["typed_runtime_unavailable_reason"] is None
    assert result["execution_record"] is None


@pytest.mark.asyncio
async def test_s1c_activated_deployment_cannot_downgrade_failed_auth() -> None:
    # Test D: once the factory IS wired, a failed-auth request STOPS.  The graph
    # has NO edge from typed_runtime_unavailable back to context/model, so an
    # individual request cannot be downgraded to the legacy/no-auth path.
    deployment = _Deployment(_release(_active_metrics()), _snapshot(_relation()))
    gateway, execute = _gateway_with_rows()
    model_gateway, provider = _model_gateway_with_provider()
    spy = _RuntimeSpy(deployment, gateway)
    engine = create_v2_engine(
        checkpointer=MemorySaver(),
        model_gateway=model_gateway,
        typed_runtime_factory=spy,
    )

    result = await _run(
        engine,
        identity=_identity(),
        authorization=None,
        thread_id=THREAD_A,
    )

    assert result["stop_reason"] == "typed_runtime_unavailable"
    assert result["typed_runtime_unavailable_reason"] == "authorization_context_missing"
    assert result["model_receipt"] is None
    assert result["execution_record"] is None
    assert provider.calls == 0
    execute.assert_not_awaited()


# --------------------------------------------------------------------------- #
# A3 landmine lock: the S1c factory can never build the no-auth seam
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_s1c_factory_never_constructs_a_compiler_without_authority() -> None:
    deployment = _Deployment(_release(_active_metrics()), _snapshot(_relation()))
    with patch(
        "src.nl2sql.orchestration.typed_runtime.MetricQueryCompiler"
    ) as compiler_cls:
        missing = await build_request_typed_runtime(
            views=_views(),
            read_active=deployment.read_active,
            read_snapshot=deployment.read_snapshot,
            gateway=_gateway(),
            identity=_identity(),
            authorization=None,
        )
    assert isinstance(missing, TypedRuntimeUnavailable)
    assert missing.reason == "authorization_context_missing"
    assert deployment.active_calls == 0
    compiler_cls.assert_not_called()


# --------------------------------------------------------------------------- #
# Engine guard: the factory participates in BOTH guard tuples
# --------------------------------------------------------------------------- #


def test_s1c_factory_wiring_is_all_or_none() -> None:
    with pytest.raises(ValueError, match="typed plan pipeline requires"):
        create_v2_engine(
            checkpointer=MemorySaver(),
            model_gateway=_model_gateway(),
            context_resolver=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="cannot be combined with static"):
        create_v2_engine(
            checkpointer=MemorySaver(),
            model_gateway=_model_gateway(),
            typed_runtime_factory=_RuntimeSpy(  # type: ignore[arg-type]
                _Deployment(_release(_active_metrics()), _snapshot(_relation())),
                _gateway(),
            ),
            query_plan_provider=object(),  # type: ignore[arg-type]
        )
    # A factory alone is a complete pipeline.
    engine = _engine(
        _RuntimeSpy(
            _Deployment(_release(_active_metrics()), _snapshot(_relation())),
            _gateway(),
        )
    )
    assert engine is not None


# --------------------------------------------------------------------------- #
# V1 sensitivity: no second business-field gate, and no access decision on scope
# --------------------------------------------------------------------------- #


def test_s1c_restricted_data_stays_non_authoritative_and_scope_independent() -> None:
    # V1: ordinary authorized business data inside the effective organization
    # scope is default-allowed, so the typed path introduces NO second
    # business-field sensitivity gate.  restricted_data stays a non-authoritative
    # routing signal and is NEVER derived from organization scope_level.
    signals = _signals(QUESTION, context=_context_bundle())
    assert signals.restricted_data is False
    assert not hasattr(signals, "scope_level")
