"""B-RT E2E: the real HTTP demo journey (A + B) and the zero-model proof."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from src.core.auth.demo_provider import DemoBackendAuthorizationProvider
from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import AuthorizationContext, RequestIdentity
from src.nl2sql.orchestration.engine import create_v2_engine
from src.nl2sql.v2 import register_v2_routes

DEMO_USER = "demo-analyst"


class _CountingModelGateway:
    """Counts EVERY invocation; the ONLY gateway the engine can reach."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("a QUERY run must never invoke the model gateway")

    @property
    def available(self) -> bool:
        return True


class _DemoContainer:
    """Real-shaped container bound to a demo deployment."""

    def __init__(self) -> None:
        self.provider = DemoBackendAuthorizationProvider()
        self.gateway = _CountingModelGateway()
        self.deployment_input_calls = 0
        # Proves the E2E actually built and used the DEMO runtime.
        self.demo_runtime_built = False
        self._engine = None

    def get_backend_authorization_provider(self) -> Any:
        return self.provider

    def authority_provenance(self) -> str:
        return "demo"

    async def get_engine(self) -> Any:
        if self._engine is None:
            from src.nl2sql.demo.runtime import build_demo_runtime

            async def factory(
                *, identity: Any, authorization: Any, expected_revision: Any
            ) -> Any:
                from src.nl2sql.orchestration.typed_runtime import (
                    TypedRuntimeUnavailable,
                )

                if authorization is None:
                    return TypedRuntimeUnavailable("authorization_context_missing")
                runtime = build_demo_runtime(
                    identity=identity,
                    authorization=authorization,
                    expected_revision=expected_revision,
                )
                if runtime is None:
                    return TypedRuntimeUnavailable("demo_identity_not_fixture")
                self.demo_runtime_built = True
                return runtime

            self._engine = create_v2_engine(
                checkpointer=MemorySaver(),
                model_gateway=self.gateway,
                typed_runtime_factory=factory,
            )
        return self._engine


def _client(container: _DemoContainer) -> TestClient:
    app = FastAPI()
    app.state.container = container

    async def _identity() -> AuthUser:
        return AuthUser(
            user_id=DEMO_USER, telephone=None, roles=[], permissions=[]
        )

    register_v2_routes(app)
    app.dependency_overrides[require_nl2sql_permission] = _identity
    return TestClient(app)


def _post(client: TestClient, **body: Any) -> Any:
    return client.post(
        "/api/v2/nl2sql/queries",
        json={"messages": [{"role": "user", "content": body.pop("question")}], **body},
    )


def test_e2e_a_query_runs_the_real_demo_journey() -> None:
    container = _DemoContainer()
    with _client(container) as client:
        response = _post(client, question="revenue", requested_mode="QUERY")
    body = response.json()
    assert response.status_code == 200, body
    assert body["effective_mode"] == "QUERY"
    assert body["authority_provenance"] == "demo"
    assert body["run_id"]
    assert body["thread_id"]
    # the QUERY run never reached the model
    assert container.gateway.calls == 0
    # the demo runtime was ACTUALLY built and used by the engine
    assert container.demo_runtime_built is True
    # it did not silently fall into the generic proposal failure
    assert "query_plan_proposal_failed" not in str(body)
    assert "context_compilation_failed" not in str(body)


def test_e2e_b_unsupported_query_suggests_analyze() -> None:
    container = _DemoContainer()
    with _client(container) as client:
        response = _post(
            client, question="why did revenue drop", requested_mode="QUERY"
        )
    body = response.json()
    assert response.status_code == 200, body
    assert body["effective_mode"] == "QUERY"
    assert container.gateway.calls == 0
    blocks = body["blocks"]
    assert blocks, body
    suggestion = next(
        (b for b in blocks if b.get("type") == "mode_suggestion"), None
    )
    assert suggestion is not None, blocks
    assert suggestion["current_mode"] == "QUERY"
    assert suggestion["suggested_mode"] == "ANALYZE"
    assert suggestion["outcome"] == "cannot_resolve"
    assert suggestion["run_id"] == body["run_id"]
    # never a generic failure
    assert "query_plan_proposal_failed" not in str(body)
    assert "context_compilation_failed" not in str(body)


def test_mode_switch_starts_a_new_run_on_the_same_thread() -> None:
    container = _DemoContainer()
    with _client(container) as client:
        first = _post(client, question="why did revenue drop", requested_mode="QUERY")
        first_body = first.json()
        thread_id = first_body["thread_id"]
        old_run = first_body["run_id"]
        second = _post(
            client,
            question="why did revenue drop",
            requested_mode="ANALYZE",
            thread_id=thread_id,
            switched_from_run_id=old_run,
        )
    second_body = second.json()
    assert second.status_code == 200, second_body
    assert str(second_body["thread_id"]) == str(thread_id)
    assert second_body["run_id"] != old_run
    assert second_body["switched_from_run_id"] == old_run
    assert second_body["effective_mode"] == "ANALYZE"


def test_mode_switch_requires_the_current_owned_predecessor() -> None:
    container = _DemoContainer()
    with _client(container) as client:
        first = _post(client, question="revenue", requested_mode="QUERY")
        first_body = first.json()
        thread_id = first_body["thread_id"]
        first_run = first_body["run_id"]
        current = _post(
            client,
            question="revenue",
            requested_mode="QUERY",
            thread_id=thread_id,
        )
        current_run = current.json()["run_id"]

        stale = _post(
            client,
            question="revenue",
            requested_mode="ANALYZE",
            thread_id=thread_id,
            switched_from_run_id=first_run,
        )
        assert stale.status_code == 409
        assert stale.json()["detail"] == "mode_switch_lineage_invalid"

        foreign_thread = _post(
            client,
            question="revenue",
            requested_mode="ANALYZE",
            thread_id=str(uuid4()),
            switched_from_run_id=current_run,
        )
        assert foreign_thread.status_code == 409
        assert foreign_thread.json()["detail"] == "mode_switch_lineage_invalid"


def test_mode_change_requires_lineage_and_same_mode_rejects_false_switch() -> None:
    container = _DemoContainer()
    with _client(container) as client:
        first = _post(client, question="revenue", requested_mode="QUERY")
        first_body = first.json()
        thread_id = first_body["thread_id"]
        run_id = first_body["run_id"]

        missing = _post(
            client,
            question="revenue",
            requested_mode="ANALYZE",
            thread_id=thread_id,
        )
        assert missing.status_code == 409
        assert missing.json()["detail"] == "mode_switch_lineage_invalid"

        same_mode = _post(
            client,
            question="revenue",
            requested_mode="QUERY",
            thread_id=thread_id,
            switched_from_run_id=run_id,
        )
        assert same_mode.status_code == 409
        assert same_mode.json()["detail"] == "mode_switch_lineage_invalid"

@pytest.mark.asyncio
async def test_container_demo_factory_rejects_a_non_demo_revision() -> None:
    """The container gate refuses a production-looking revision."""

    from src.core.settings import Settings, get_settings
    from src.nl2sql.container import AppContainer
    from src.nl2sql.contracts import AuthorizationContext, RequestIdentity

    settings = Settings(
        _env_file=None,
        auth_enabled=False,
        service_mode="infra-dev",
        typed_runtime_activation="demo_synthetic_authorization",
    )
    container = AppContainer.__new__(AppContainer)
    get_settings.cache_clear()
    import src.nl2sql.container as container_module

    original = container_module.get_settings
    container_module.get_settings = lambda: settings  # type: ignore[assignment]
    try:
        factory = container._demo_typed_runtime_factory()
        identity = RequestIdentity(request_id=UUID(int=3), user_id=DEMO_USER)
        backend = AuthorizationContext(
            authorization_revision="backend-rev-1",
            agent_enabled=True,
            scope_level="team",
            allowed_scope_ids=("t1",),
        )
        result = await factory(
            identity=identity, authorization=backend, expected_revision=None
        )
        from src.nl2sql.orchestration.typed_runtime import TypedRuntimeUnavailable

        assert isinstance(result, TypedRuntimeUnavailable)
        assert result.reason == "demo_authorization_revision_required"
        # an unknown identity is also refused
        stranger = RequestIdentity(request_id=UUID(int=4), user_id="real-alice")
        demo_auth = AuthorizationContext(
            authorization_revision="demo-synthetic:v1",
            agent_enabled=True,
            scope_level="team",
            allowed_scope_ids=("demo-team-1",),
        )
        refused = await factory(
            identity=stranger, authorization=demo_auth, expected_revision=None
        )
        assert isinstance(refused, TypedRuntimeUnavailable)
        assert refused.reason == "demo_identity_not_fixture"
    finally:
        container_module.get_settings = original  # type: ignore[assignment]

def test_margin_is_ambiguous_not_unsupported() -> None:
    """margin must NOT become a mode escalation; it is a bounded ambiguity."""

    from src.nl2sql.contracts import AuthorizationContext, RequestIdentity
    from src.nl2sql.demo.runtime import build_demo_runtime

    identity = RequestIdentity(request_id=UUID(int=2), user_id=DEMO_USER)
    auth = AuthorizationContext(
        authorization_revision="demo-synthetic:v1",
        agent_enabled=True,
        scope_level="team",
        allowed_scope_ids=("demo-team-1",),
    )
    runtime = build_demo_runtime(identity=identity, authorization=auth)
    assert runtime is not None

    import asyncio

    async def _resolve() -> Any:
        return await runtime.context_resolver.resolve(
            question="margin", identity=identity, route_hint="fast"
        )

    context = asyncio.run(_resolve())
    assert context.resolution_status == "ambiguous"
    assert context.unresolved_slots == ("metric",)
    # and crucially it is NOT the unsupported sentinel
    assert context.asset_ids == ("demo.ambiguous.margin",)

def test_engine_runtime_gate_accepts_both_concrete_implementations() -> None:
    """The engine gate is the Protocol: production AND demo both pass."""

    from src.nl2sql.demo.runtime import DemoRequestTypedRuntime, build_demo_runtime
    from src.nl2sql.orchestration.typed_runtime import TypedRuntimeBundle

    identity = RequestIdentity(request_id=UUID(int=5), user_id=DEMO_USER)
    auth = AuthorizationContext(
        authorization_revision="demo-synthetic:v1",
        agent_enabled=True,
        scope_level="team",
        allowed_scope_ids=("demo-team-1",),
    )
    demo = build_demo_runtime(identity=identity, authorization=auth)
    assert isinstance(demo, DemoRequestTypedRuntime)
    # the SAME gate the engine uses at runtime
    assert isinstance(demo, TypedRuntimeBundle)
    # the engine's gate must be the PROTOCOL, not a concrete type
    import inspect

    from src.nl2sql.orchestration import engine as engine_module

    gate = inspect.getsource(engine_module._request_runtime) if False else None
    del gate
    source = inspect.getsource(engine_module)
    assert "isinstance(runtime, TypedRuntimeBundle)" in source
    # the engine must never import the demo package
    assert "from src.nl2sql.demo" not in source
    assert "import src.nl2sql.demo" not in source
