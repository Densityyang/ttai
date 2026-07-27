from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import RequestContext, RequestIdentity
from src.nl2sql.ownership import internal_thread_id, runtime_config
from src.nl2sql.v2 import QueryRequest, register_v1_gone_routes, register_v2_routes

THREAD_ID = UUID("11111111-1111-1111-1111-111111111111")


class FakeSupervisor:
    def __init__(self, owned_thread: str) -> None:
        self.owned_thread = owned_thread

    async def aget_state(self, config: dict[str, object]) -> SimpleNamespace:
        thread_id = config["configurable"]["thread_id"]  # type: ignore[index]
        if thread_id != self.owned_thread:
            return SimpleNamespace(values={}, config={}, metadata={})
        return SimpleNamespace(
            values={"messages": [HumanMessage(content="private history")]},
            config={"configurable": {"checkpoint_id": "checkpoint-a"}},
            metadata={"created_at": "2026-01-01T00:00:00Z"},
        )

    async def aget_state_history(self, config: dict[str, object]):
        state = await self.aget_state(config)
        if state.values:
            yield state


class FakeContainer:
    def __init__(self, supervisor: FakeSupervisor) -> None:
        self._supervisor = supervisor

    async def get_supervisor(self) -> FakeSupervisor:
        return self._supervisor


def _context_for(user_id: str) -> RequestContext:
    return RequestContext(
        identity=RequestIdentity(
            request_id=UUID("22222222-2222-2222-2222-222222222222"),
            user_id=user_id,
            roles=frozenset({"analyst"}),
            permissions=frozenset({"nl2sql:invoke"}),
        ),
        thread_id=THREAD_ID,
        trace_id="trace-1",
    )


def _app_for(supervisor: FakeSupervisor) -> FastAPI:
    app = FastAPI()
    app.state.container = FakeContainer(supervisor)
    register_v2_routes(app)
    register_v1_gone_routes(app)
    return app


def test_thread_state_is_namespaced_by_owner() -> None:
    alice = AuthUser(user_id="alice", telephone=None, roles=["analyst"], permissions=["*"])
    bob = AuthUser(user_id="bob", telephone=None, roles=["analyst"], permissions=["*"])
    app = _app_for(FakeSupervisor(internal_thread_id(_context_for("alice"))))

    async def alice_dependency() -> AuthUser:
        return alice

    app.dependency_overrides[require_nl2sql_permission] = alice_dependency
    with TestClient(app) as client:
        response = client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}")
    assert response.status_code == 200
    assert response.json()["messages"][0]["content"] == "private history"

    async def bob_dependency() -> AuthUser:
        return bob

    app.dependency_overrides[require_nl2sql_permission] = bob_dependency
    with TestClient(app) as client:
        response = client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}")
    assert response.status_code == 404


def test_legacy_routes_return_migration_error() -> None:
    app = _app_for(FakeSupervisor(internal_thread_id(_context_for("alice"))))
    with TestClient(app) as client:
        response = client.post("/nl2sql/invoke")
    assert response.status_code == 410
    assert response.json()["code"] == "API_V1_GONE"


def test_query_payload_limits_and_tenant_rejection() -> None:
    with pytest.raises(ValidationError, match="8 KiB"):
        QueryRequest.model_validate({"messages": [{"role": "user", "content": "x" * 8193}]})

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "hello"}],
                "tenant_id": "client-controlled-tenant",
            }
        )


def test_thread_namespace_is_safe_for_user_ids_with_delimiters() -> None:
    namespaced = internal_thread_id(_context_for("alice:engineering"))
    assert namespaced == f"default:alice%3Aengineering:{THREAD_ID}"


def test_runtime_config_propagates_request_identity_to_subgraphs() -> None:
    config = runtime_config(_context_for("alice"))
    configurable = cast(dict[str, object], config["configurable"])
    assert configurable["thread_id"] == f"default:alice:{THREAD_ID}"
    identity = cast(dict[str, str], configurable["request_identity"])
    context = cast(dict[str, str], configurable["request_context"])
    assert identity["user_id"] == "alice"
    assert context["thread_id"] == str(THREAD_ID)
