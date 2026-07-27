from __future__ import annotations

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
from src.nl2sql.infra.llm.gateway import (
    FakeProvider,
    ModelGateway,
    ModelProfile,
    ModelTarget,
    ProviderResponse,
)
from src.nl2sql.orchestration.engine import create_v2_engine
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
    assert len(resumed["messages"]) == 2


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
