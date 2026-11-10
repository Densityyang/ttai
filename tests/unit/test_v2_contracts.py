from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

import src.nl2sql.v2 as v2
from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.contracts import RequestContext, RequestIdentity
from src.nl2sql.ownership import internal_thread_id, runtime_config
from src.nl2sql.v2 import QueryRequest, register_v1_gone_routes, register_v2_routes

THREAD_ID = UUID("11111111-1111-1111-1111-111111111111")


class FakeEngine:
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

    async def aget_state_history(
        self,
        config: dict[str, object],
        *,
        before: dict[str, object] | None = None,
        limit: int | None = None,
    ):
        del before, limit
        state = await self.aget_state(config)
        if state.values:
            yield state


class PagedHistoryEngine(FakeEngine):
    def __init__(self, owned_thread: str, checkpoint_ids: list[str]) -> None:
        super().__init__(owned_thread)
        self.checkpoints = [
            SimpleNamespace(
                values={"messages": [HumanMessage(content=f"message-{checkpoint_id}")]},
                config={"configurable": {"checkpoint_id": checkpoint_id}},
                metadata={"created_at": f"2026-01-01T00:00:{index:02d}Z"},
            )
            for index, checkpoint_id in enumerate(checkpoint_ids)
        ]
        self.history_requests: list[tuple[str | None, int | None]] = []
        self.history_yield_count = 0

    async def aget_state(self, config: dict[str, object]) -> SimpleNamespace:
        configurable = cast(dict[str, object], config["configurable"])
        thread_id = configurable["thread_id"]
        if thread_id != self.owned_thread:
            return SimpleNamespace(values={}, config={}, metadata={})
        requested_checkpoint_id = configurable.get("checkpoint_id")
        if requested_checkpoint_id is None:
            return self.checkpoints[0] if self.checkpoints else SimpleNamespace(
                values={}, config={}, metadata={}
            )
        for state in self.checkpoints:
            if state.config["configurable"]["checkpoint_id"] == requested_checkpoint_id:
                return state
        return SimpleNamespace(values={}, config={}, metadata={})

    async def aget_state_history(
        self,
        config: dict[str, object],
        *,
        before: dict[str, object] | None = None,
        limit: int | None = None,
    ):
        configurable = cast(dict[str, object], config["configurable"])
        if configurable["thread_id"] != self.owned_thread:
            return
        before_id: str | None = None
        if before is not None:
            before_config = cast(dict[str, object], before.get("configurable", {}))
            raw_before_id = before_config.get("checkpoint_id")
            before_id = str(raw_before_id) if raw_before_id is not None else None
        self.history_requests.append((before_id, limit))
        checkpoints = self.checkpoints
        if before_id is not None:
            before_index = next(
                (
                    index
                    for index, state in enumerate(checkpoints)
                    if state.config["configurable"]["checkpoint_id"] == before_id
                ),
                None,
            )
            if before_index is None:
                return
            checkpoints = checkpoints[before_index + 1 :]
        if limit is not None:
            checkpoints = checkpoints[:limit]
        for state in checkpoints:
            self.history_yield_count += 1
            yield state


@pytest.fixture(autouse=True)
def _stable_history_cursor_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Product-like stable shared key for route tests; no real secret or DSN is used.
    monkeypatch.setattr(
        v2,
        "get_settings",
        lambda: SimpleNamespace(
            service_mode="product",
            checkpoint_database_url="synthetic-history-cursor-key",
            control_database_url=None,
        ),
    )


class CapturingEngine(FakeEngine):
    def __init__(self, owned_thread: str) -> None:
        super().__init__(owned_thread)
        self.last_config: dict[str, object] | None = None

    async def ainvoke(
        self,
        values: dict[str, object],
        config: dict[str, object],
    ) -> dict[str, object]:
        del values
        self.last_config = config
        return {"messages": [AIMessage(content="ok")]}


class FakeContainer:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    async def get_engine(self) -> FakeEngine:
        return self._engine


class MissingEngineContainer:
    def readiness_report(self, *, model_available: bool) -> dict[str, str]:
        del model_available
        return {"status": "ready"}


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


def _app_for(engine: FakeEngine) -> FastAPI:
    app = FastAPI()
    app.state.container = FakeContainer(engine)
    register_v2_routes(app)
    register_v1_gone_routes(app)
    return app


def _app_for_history_user(engine: FakeEngine, user_id: str = "alice") -> FastAPI:
    app = _app_for(engine)

    async def user_dependency() -> AuthUser:
        return AuthUser(
            user_id=user_id,
            telephone=None,
            roles=["analyst"],
            permissions=["nl2sql:invoke"],
        )

    app.dependency_overrides[require_nl2sql_permission] = user_dependency
    return app


def test_thread_state_is_namespaced_by_owner() -> None:
    alice = AuthUser(user_id="alice", telephone=None, roles=["analyst"], permissions=["*"])
    bob = AuthUser(user_id="bob", telephone=None, roles=["analyst"], permissions=["*"])
    app = _app_for(FakeEngine(internal_thread_id(_context_for("alice"))))

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


def test_history_uses_compatible_default_page_and_paginates_without_duplicates() -> None:
    checkpoint_ids = [f"cp-{index:03d}" for index in range(24, -1, -1)]
    engine = PagedHistoryEngine(internal_thread_id(_context_for("alice")), checkpoint_ids)
    app = _app_for_history_user(engine)

    with TestClient(app) as client:
        first = client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}/history")
        first_body = first.json()
        second = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"cursor": first_body["next_cursor"]},
        )

    assert first.status_code == 200
    assert first_body["limit"] == v2.HISTORY_DEFAULT_PAGE_SIZE
    assert len(first_body["snapshots"]) == 20
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]
    assert second.status_code == 200
    assert second.json()["limit"] == 20
    assert second.json()["has_more"] is False
    observed = [
        snapshot["checkpoint_id"]
        for snapshot in first_body["snapshots"] + second.json()["snapshots"]
    ]
    assert observed == checkpoint_ids
    assert len(observed) == len(set(observed))
    assert engine.history_requests == [(None, 21), ("cp-005", 21)]


def test_history_limit_boundaries_and_legacy_empty_single_page_responses() -> None:
    engine = PagedHistoryEngine(internal_thread_id(_context_for("alice")), ["only-checkpoint"])
    app = _app_for_history_user(engine)
    with TestClient(app) as client:
        low = client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}/history?limit=0")
        high = client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}/history?limit=101")
        single = client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}/history")

    assert low.status_code == 422
    assert high.status_code == 422
    assert single.status_code == 200
    assert single.json()["thread_id"] == str(THREAD_ID)
    assert single.json()["snapshots"][0]["checkpoint_id"] == "only-checkpoint"
    assert single.json()["next_cursor"] is None
    assert single.json()["has_more"] is False

    empty_app = _app_for_history_user(PagedHistoryEngine(
        internal_thread_id(_context_for("alice")), []
    ))
    with TestClient(empty_app) as client:
        empty = client.get(f"/api/v2/nl2sql/threads/{THREAD_ID}/history")
    assert empty.status_code == 404


def test_history_rejects_tampered_foreign_and_limit_mismatched_cursors() -> None:
    engine = PagedHistoryEngine(
        internal_thread_id(_context_for("alice")),
        ["cp-02", "cp-01", "cp-00"],
    )
    app = _app_for_history_user(engine)
    with TestClient(app) as client:
        first = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"limit": 2},
        )
        token = first.json()["next_cursor"]
        bad_signature = token[:-1] + ("A" if token[-1] != "A" else "B")
        tampered = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"cursor": bad_signature},
        )
        changed_limit = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"cursor": token, "limit": 1},
        )
        malformed = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"cursor": "not-a-valid-cursor"},
        )
    assert first.status_code == 200
    assert tampered.status_code == 422
    assert changed_limit.status_code == 422
    assert malformed.status_code == 422

    bob_app = _app_for_history_user(engine, user_id="bob")
    with TestClient(bob_app) as client:
        foreign = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"cursor": token},
        )
    assert foreign.status_code == 404
    assert foreign.json() == {"detail": "thread not found"}


def test_history_refuses_stale_cursor_anchor() -> None:
    engine = PagedHistoryEngine(
        internal_thread_id(_context_for("alice")),
        ["cp-02", "cp-01", "cp-00"],
    )
    app = _app_for_history_user(engine)
    with TestClient(app) as client:
        first = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"limit": 2},
        )
        token = first.json()["next_cursor"]
    cursor = v2._decode_history_cursor(token, thread_id=THREAD_ID, owner_user_id="alice")
    engine.checkpoints = [
        state
        for state in engine.checkpoints
        if state.config["configurable"]["checkpoint_id"] != cursor.before_checkpoint_id
    ]

    with TestClient(app) as client:
        stale = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"cursor": token},
        )
    assert stale.status_code == 409
    assert stale.json() == {"detail": "history cursor stale"}


def test_history_checkpoint_mutation_between_pages_keeps_original_order() -> None:
    engine = PagedHistoryEngine(
        internal_thread_id(_context_for("alice")),
        ["cp-05", "cp-04", "cp-03", "cp-02", "cp-01", "cp-00"],
    )
    app = _app_for_history_user(engine)
    with TestClient(app) as client:
        first = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"limit": 2},
        )
        engine.checkpoints.insert(
            0,
            SimpleNamespace(
                values={"messages": [HumanMessage(content="new checkpoint")]},
                config={"configurable": {"checkpoint_id": "cp-06"}},
                metadata={"created_at": "2026-01-02T00:00:00Z"},
            ),
        )
        second = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"cursor": first.json()["next_cursor"]},
        )

    assert [item["checkpoint_id"] for item in first.json()["snapshots"]] == [
        "cp-05",
        "cp-04",
    ]
    assert [item["checkpoint_id"] for item in second.json()["snapshots"]] == [
        "cp-03",
        "cp-02",
    ]
    assert second.json()["snapshots"][0]["checkpoint_id"] != "cp-06"


def test_history_large_synthetic_source_is_bounded_and_projection_is_truncated() -> None:
    checkpoint_ids = [f"cp-{index:05d}" for index in range(4_999, -1, -1)]
    engine = PagedHistoryEngine(internal_thread_id(_context_for("alice")), checkpoint_ids)
    engine.checkpoints[0].values = {
        "messages": [HumanMessage(content="x" * 20_000) for _ in range(150)]
    }
    app = _app_for_history_user(engine)

    with TestClient(app) as client:
        response = client.get(
            f"/api/v2/nl2sql/threads/{THREAD_ID}/history",
            params={"limit": 100},
        )

    body = response.json()
    assert response.status_code == 200
    assert len(body["snapshots"]) == 100
    assert body["has_more"] is True
    assert body["snapshots"][0]["messages_truncated"] is True
    assert len(response.content) <= v2.HISTORY_MAX_RESPONSE_BYTES
    assert engine.history_requests == [(None, 101)]
    assert engine.history_yield_count == 101


def test_legacy_routes_return_migration_error() -> None:
    app = _app_for(FakeEngine(internal_thread_id(_context_for("alice"))))
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


def test_query_payload_limits_count_total_and_utf8_bytes() -> None:
    QueryRequest.model_validate(
        {"messages": [{"role": "user", "content": "界" * 2730}]}
    )

    with pytest.raises(ValidationError, match="8 KiB"):
        QueryRequest.model_validate(
            {"messages": [{"role": "user", "content": "界" * 2731}]}
        )

    with pytest.raises(ValidationError, match="at most 20 items"):
        QueryRequest.model_validate(
            {"messages": [{"role": "user", "content": "x"}] * 21}
        )

    with pytest.raises(ValidationError, match="total at most 32 KiB"):
        QueryRequest.model_validate(
            {"messages": [{"role": "user", "content": "x" * 7000}] * 5}
        )


def test_query_rejects_a_container_without_the_typed_engine_boundary() -> None:
    app = FastAPI()
    app.state.container = MissingEngineContainer()
    register_v2_routes(app)

    async def user_dependency() -> AuthUser:
        return AuthUser(
            user_id="alice",
            telephone=None,
            roles=["analyst"],
            permissions=["nl2sql:invoke"],
        )

    app.dependency_overrides[require_nl2sql_permission] = user_dependency
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            json={"messages": [{"role": "user", "content": "show revenue"}]},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "runtime engine unavailable"}


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


def test_query_api_propagates_authenticated_identity_to_runtime() -> None:
    engine = CapturingEngine(internal_thread_id(_context_for("alice")))
    app = _app_for(engine)

    async def alice_dependency() -> AuthUser:
        return AuthUser(
            user_id="alice",
            telephone=None,
            roles=["analyst"],
            permissions=["nl2sql:invoke"],
        )

    app.dependency_overrides[require_nl2sql_permission] = alice_dependency
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            headers={"x-request-id": "22222222-2222-2222-2222-222222222222"},
            json={
                "thread_id": str(THREAD_ID),
                "messages": [{"role": "user", "content": "show revenue"}],
            },
        )

    assert response.status_code == 200
    assert engine.last_config is not None
    configurable = cast(dict[str, object], engine.last_config["configurable"])
    identity = cast(dict[str, object], configurable["request_identity"])
    assert configurable["thread_id"] == f"default:alice:{THREAD_ID}"
    assert identity == {
        "request_id": "22222222-2222-2222-2222-222222222222",
        "user_id": "alice",
        "roles": ["analyst"],
        "permissions": ["nl2sql:invoke"],
        "auth_epoch": None,
    }


def test_capabilities_explain_when_codeact_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.nl2sql.config.settings import AgentConfig
    from src.nl2sql.infra.llm import gateway

    app = _app_for(FakeEngine(internal_thread_id(_context_for("alice"))))
    monkeypatch.setattr(v2, "get_agent_config", lambda: AgentConfig(_env_file=None))
    monkeypatch.setattr(gateway, "model_gateway_available", lambda: True)

    async def alice_dependency() -> AuthUser:
        return AuthUser(user_id="alice", telephone=None, roles=["analyst"], permissions=["*"])

    app.dependency_overrides[require_nl2sql_permission] = alice_dependency
    with TestClient(app) as client:
        response = client.get("/api/v2/nl2sql/capabilities")

    assert response.status_code == 200
    assert response.json()["codeact"] is False
    # R8: the default deployment must not claim the typed runtime is active.
    assert response.json()["typed_runtime"] is False
    assert "dynamic calculation is disabled" in response.json()["degradation_reasons"]
