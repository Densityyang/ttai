"""B1-FIX: the action/resume HTTP boundary re-fetches CURRENT authorization.

An action can CONTINUE BUSINESS EXECUTION, so it must call the trusted Backend
provider again.  Read-only thread/history routes must NOT.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import AuthorizationContext
from src.nl2sql.ownership import AUTHORIZATION_CONFIG_KEY, authorization_context_from_config
from src.nl2sql.v2 import register_v2_routes

THREAD_ID = UUID("11111111-1111-1111-1111-111111111111")


def _user() -> AuthUser:
    return AuthUser(user_id="alice", telephone=None, roles=["analyst"], permissions=["*"])


def _context(revision: str) -> AuthorizationContext:
    return AuthorizationContext(
        authorization_revision=revision,
        agent_enabled=True,
        scope_level="team",
        allowed_scope_ids=("t1",),
    )


class _CountingProvider:
    """Counts how many times the trusted Backend provider was consulted."""

    def __init__(self, context: AuthorizationContext | None) -> None:
        self.context = context
        self.calls = 0

    async def load(self, user: AuthUser) -> AuthorizationContext | None:
        del user
        self.calls += 1
        return self.context


class _Engine:
    def __init__(self) -> None:
        self.invoke_config: dict[str, Any] | None = None

    async def ainvoke(self, values: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        del values
        self.invoke_config = config
        # a well-formed legacy action result, so the route returns 200
        return {
            "messages": [],
            "hitl_status": "approved",
            "hitl_version": 2,
            "needs_hitl": False,
        }

    async def aget_state(self, config: dict[str, Any]) -> SimpleNamespace:
        del config
        # a thread awaiting an action, so the route proceeds past the guards
        return SimpleNamespace(
            values={"messages": [], "hitl_status": "awaiting_action", "hitl_version": 1},
            config={},
            metadata={},
        )

    async def aget_state_history(self, config: dict[str, Any]):
        del config
        yield SimpleNamespace(values={"messages": []}, config={}, metadata={})


class _Container:
    def __init__(self, engine: _Engine, provider: Any) -> None:
        self._engine = engine
        self._provider = provider

    async def get_engine(self) -> Any:
        return self._engine

    def get_backend_authorization_provider(self) -> Any:
        return self._provider


def _client(engine: _Engine, provider: Any) -> TestClient:
    app = FastAPI()
    app.state.container = _Container(engine, provider)

    async def _identity() -> AuthUser:
        return _user()

    register_v2_routes(app)
    app.dependency_overrides[require_nl2sql_permission] = _identity
    return TestClient(app)


def _action_payload() -> dict[str, Any]:
    return {"action": "approve", "idempotency_key": "k1", "expected_version": 1}


def test_action_route_refetches_current_authorization() -> None:
    """The resume boundary must consult the trusted provider again."""

    engine = _Engine()
    provider = _CountingProvider(_context("rev-2"))
    with _client(engine, provider) as client:
        response = client.post(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/actions", json=_action_payload()
        )
    assert response.status_code == 200
    # the CURRENT Backend authorization was genuinely re-fetched
    assert provider.calls == 1
    assert engine.invoke_config is not None
    configurable = engine.invoke_config["configurable"]
    current = authorization_context_from_config(configurable)
    assert current is not None
    assert current.authorization_revision == "rev-2"


def test_history_route_does_not_refetch_authorization() -> None:
    """Read-only history must NOT trigger a Backend authorization refresh."""

    engine = _Engine()
    provider = _CountingProvider(_context("rev-2"))
    with _client(engine, provider) as client:
        response = client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}/history")
    assert response.status_code == 200
    assert provider.calls == 0


def test_missing_current_authorization_yields_no_authority_carrier() -> None:
    """Provider returns None -> the run carries no authority (fails closed)."""

    engine = _Engine()
    provider = _CountingProvider(None)
    with _client(engine, provider) as client:
        response = client.post(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/actions", json=_action_payload()
        )
    assert response.status_code == 200
    assert provider.calls == 1
    assert engine.invoke_config is not None
    assert AUTHORIZATION_CONFIG_KEY not in engine.invoke_config["configurable"]


@pytest.mark.parametrize(
    "field",
    ["authorization_context", "authorization", "allowed_scope_ids", "scope_level"],
)
def test_client_cannot_inject_authorization_through_the_body(field: str) -> None:
    """No body field may become the execution authority."""

    engine = _Engine()
    provider = _CountingProvider(_context("rev-server"))
    payload = _action_payload()
    payload[field] = {
        "authorization_revision": "rev-forged",
        "agent_enabled": True,
        "scope_level": "city_company",
        "allowed_scope_ids": ["*"],
    }
    with _client(engine, provider) as client:
        response = client.post(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/actions", json=payload
        )
    # The injected field is not part of the action contract, so the request is
    # rejected before any engine invocation: it can never become the authority.
    assert response.status_code in {200, 400, 409, 422}
    if response.status_code == 200:
        assert engine.invoke_config is not None
        configurable = engine.invoke_config["configurable"]
        current = authorization_context_from_config(configurable)
        if current is not None:
            assert current.authorization_revision == "rev-server"
        assert provider.calls == 1
    else:
        # fail-closed: no invocation happened at all
        assert engine.invoke_config is None
        assert response.status_code == 422
