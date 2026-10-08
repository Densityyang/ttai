"""P2-S3 trusted Backend authorization carrier seam (new-run resolution only)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.nl2sql.v2 as v2
from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.provider import load_authorization, resolve_authorization_context
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import AUTHORIZATION_DENIED, AuthorizationContext
from src.nl2sql.ownership import AUTHORIZATION_CONFIG_KEY, authorization_context_from_config
from src.nl2sql.v2 import register_v2_routes

THREAD_ID = UUID("11111111-1111-1111-1111-111111111111")


def _context(revision: str = "rev-1") -> AuthorizationContext:
    return AuthorizationContext(
        authorization_revision=revision,
        agent_enabled=True,
        scope_level="team",
        allowed_scope_ids=("t1",),
    )


def _user() -> AuthUser:
    return AuthUser(user_id="alice", telephone=None, roles=["admin"], permissions=["*"])


class _StubProvider:
    def __init__(self, result: Any = None, *, raises: bool = False) -> None:
        self.result = result
        self.raises = raises
        self.calls = 0

    async def load(self, user: AuthUser) -> Any:
        del user
        self.calls += 1
        if self.raises:
            raise RuntimeError("provider unavailable")
        return self.result


class _Lookalike:
    authorization_revision = "rev-1"
    agent_enabled = True
    scope_level = "team"
    allowed_scope_ids = ("t1",)


@pytest.mark.asyncio
async def test_resolver_returns_the_context_verbatim() -> None:
    context = _context()
    assert await resolve_authorization_context(_StubProvider(context), _user()) is context


@pytest.mark.asyncio
async def test_resolver_collapses_absent_provider_to_none() -> None:
    assert await resolve_authorization_context(None, _user()) is None


@pytest.mark.asyncio
async def test_resolver_collapses_none_result_to_none() -> None:
    assert await resolve_authorization_context(_StubProvider(None), _user()) is None


@pytest.mark.asyncio
async def test_resolver_collapses_provider_exception_to_none() -> None:
    assert await resolve_authorization_context(_StubProvider(raises=True), _user()) is None


@pytest.mark.asyncio
async def test_load_authorization_deny_shape_is_unchanged_after_refactor() -> None:
    for provider in (
        _StubProvider(None),
        _StubProvider(raises=True),
        _StubProvider(_Lookalike()),
    ):
        decision = await load_authorization(
            provider, _user(), expected_revision="rev-1"
        )
        assert decision.outcome == "deny"
        assert decision.reason == "authorization_denied"
        assert decision == AUTHORIZATION_DENIED

    allowed = await load_authorization(
        _StubProvider(_context("rev-allow")), _user(), expected_revision=None
    )
    assert allowed.outcome == "allow"
    assert allowed.authorization_revision == "rev-allow"


def test_auth_user_roles_and_permissions_never_become_authorization() -> None:
    assert set(AuthorizationContext.model_fields) == {
        "schema_version",
        "authorization_revision",
        "agent_enabled",
        "scope_level",
        "allowed_scope_ids",
    }

    provider = _StubProvider(None)
    engine = _CapturingEngine()

    with TestClient(_app(engine, provider)) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert engine.invoke_config is not None
    assert AUTHORIZATION_CONFIG_KEY not in engine.invoke_config["configurable"]


def test_accessor_returning_none_keeps_authorization_free_config() -> None:
    engine = _CapturingEngine()

    class _NoneProviderContainer:
        async def get_engine(self) -> Any:
            return engine

        def get_backend_authorization_provider(self) -> Any:
            return None

    app = FastAPI()
    app.state.container = _NoneProviderContainer()

    async def _identity() -> AuthUser:
        return _user()

    register_v2_routes(app)
    app.dependency_overrides[require_nl2sql_permission] = _identity

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert engine.invoke_config is not None
    configurable = engine.invoke_config["configurable"]
    assert AUTHORIZATION_CONFIG_KEY not in configurable
    assert {
        "thread_id",
        "request_identity",
        "request_context",
        "auth_user_id",
        "auth_user_roles",
        "auth_user_permissions",
    } <= set(configurable)
    assert authorization_context_from_config(configurable) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"authorization_revision": "rev-1"},
        _Lookalike(),
        "rev-1",
        42,
        None,
        object(),
    ],
)
async def test_resolver_collapses_malformed_payload_to_none(payload: Any) -> None:
    assert await resolve_authorization_context(_StubProvider(payload), _user()) is None


class _CapturingEngine:
    def __init__(self) -> None:
        self.invoke_config: dict[str, Any] | None = None

    async def ainvoke(
        self, values: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, Any]:
        del values
        self.invoke_config = config
        return {"messages": []}

    async def aget_state(self, config: dict[str, Any]) -> SimpleNamespace:
        del config
        return SimpleNamespace(values={"messages": []}, config={}, metadata={})

    async def aget_state_history(self, config: dict[str, Any]):
        del config
        yield SimpleNamespace(values={"messages": []}, config={}, metadata={})


class _FakeContainer:
    def __init__(self, engine: Any, provider: Any) -> None:
        self._engine = engine
        self._provider = provider

    async def get_engine(self) -> Any:
        return self._engine

    def get_backend_authorization_provider(self) -> Any:
        return self._provider


def _app(engine: Any, provider: Any) -> FastAPI:
    app = FastAPI()
    app.state.container = _FakeContainer(engine, provider)

    async def _identity() -> AuthUser:
        return _user()

    register_v2_routes(app)
    app.dependency_overrides[require_nl2sql_permission] = _identity
    return app


def test_query_route_resolves_fresh_authorization_once() -> None:
    provider = _StubProvider(_context("rev-query"))
    engine = _CapturingEngine()

    with TestClient(_app(engine, provider)) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert provider.calls == 1
    assert engine.invoke_config is not None
    authorization = engine.invoke_config["configurable"][AUTHORIZATION_CONFIG_KEY]
    assert authorization["authorization_revision"] == "rev-query"


def test_stream_route_resolves_fresh_authorization_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _StubProvider(_context("rev-stream"))
    engine = _CapturingEngine()
    captured: dict[str, Any] = {}

    async def _fake_stream(
        engine_arg: Any, messages: Any, config: Any, thread_id: Any, extra: Any = None
    ):
        del engine_arg, messages, thread_id, extra
        captured["config"] = config
        yield "data: {}\n\n"

    monkeypatch.setattr(v2, "_stream_query", _fake_stream)

    with TestClient(_app(engine, provider)) as client:
        response = client.post(
            "/api/v2/nl2sql/queries/stream",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert provider.calls == 1
    authorization = captured["config"]["configurable"][AUTHORIZATION_CONFIG_KEY]
    assert authorization["authorization_revision"] == "rev-stream"


def test_read_only_routes_never_fetch_authorization() -> None:
    """Read-only thread/history/feedback must NOT trigger an auth refresh.

    The /actions route is deliberately EXCLUDED here: an action can continue
    business execution, so it MUST re-fetch the CURRENT trusted authorization
    (see test_resume_authorization_refresh.py).
    """

    provider = _StubProvider(_context())
    engine = _CapturingEngine()

    with TestClient(_app(engine, provider)) as client:
        client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}")
        client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}/history")
        client.post(
            "/api/v2/nl2sql/feedback",
            json={"thread_id": str(THREAD_ID), "rating": "up"},
        )

    assert provider.calls == 0


def test_new_run_without_a_provider_has_no_synthetic_authorization() -> None:
    provider = _StubProvider(None)
    engine = _CapturingEngine()

    with TestClient(_app(engine, provider)) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert provider.calls == 1
    assert engine.invoke_config is not None
    assert AUTHORIZATION_CONFIG_KEY not in engine.invoke_config["configurable"]


def test_accessor_that_raises_collapses_to_the_authorization_free_carrier() -> None:
    engine = _CapturingEngine()

    class _RaisingAccessorContainer:
        async def get_engine(self) -> Any:
            return engine

        def get_backend_authorization_provider(self) -> Any:
            raise RuntimeError("provider factory failed")

    app = FastAPI()
    app.state.container = _RaisingAccessorContainer()

    async def _identity() -> AuthUser:
        return _user()

    register_v2_routes(app)
    app.dependency_overrides[require_nl2sql_permission] = _identity

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert engine.invoke_config is not None
    configurable = engine.invoke_config["configurable"]
    assert AUTHORIZATION_CONFIG_KEY not in configurable
    assert authorization_context_from_config(configurable) is None


def test_container_without_the_accessor_stays_authorization_free() -> None:
    engine = _CapturingEngine()

    class _LegacyContainer:
        async def get_engine(self) -> Any:
            return engine

    app = FastAPI()
    app.state.container = _LegacyContainer()

    async def _identity() -> AuthUser:
        return _user()

    register_v2_routes(app)
    app.dependency_overrides[require_nl2sql_permission] = _identity

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert engine.invoke_config is not None
    assert AUTHORIZATION_CONFIG_KEY not in engine.invoke_config["configurable"]
