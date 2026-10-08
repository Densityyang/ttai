from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.orchestration.decision_contract import (
    HITLDecision,
    HITLRequest,
    SlotBinding,
    resume_token,
)
from src.nl2sql.v2 import register_v2_routes

_THREAD = UUID("11111111-1111-1111-1111-111111111111")


def _request() -> HITLRequest:
    return HITLRequest(
        request_id="clarify-action-test",
        decision_kind="clarification",
        version=1,
        allowed_actions=("resolve", "reject", "cancel"),
        plan_sha256="a" * 64,
        context_checksum="b" * 64,
        policy_version="policy-v1",
        policy_checksum="c" * 64,
        issue_codes=("time_missing",),
        unresolved_slots=("time",),
    )


def _decision(*, request_id: str | None = None, value: str = "2026-09") -> HITLDecision:
    request = _request()
    return HITLDecision(
        request_id=request_id or request.request_id,
        request_version=request.version,
        action="resolve",
        slot_bindings=(SlotBinding(slot="time", value=value),),
        idempotency_key="typed-key-1",
    )


def _ledger(request: HITLRequest, decision: HITLDecision) -> dict[str, object]:
    token = resume_token(request=request, decision=decision)
    return {
        "request_id": request.request_id,
        "request_version": request.version,
        "request_checksum": request.checksum,
        "decision_checksum": decision.checksum,
        "resume_token_checksum": token.checksum,
        "status": "do-not-trust-this-field",
        "request": request.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "resume_token": token.model_dump(mode="json"),
    }


class _AuthorizationProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def load(self, _: AuthUser):
        self.calls += 1
        return None


class _Engine:
    def __init__(self, values: dict[str, object], decision: HITLDecision | None) -> None:
        self.values = values
        self.decision = decision
        self.invocations = 0

    async def aget_state(self, _: object) -> SimpleNamespace:
        return SimpleNamespace(values=self.values)

    async def ainvoke(self, *_: object, **__: object) -> dict[str, object]:
        self.invocations += 1
        if self.decision is None:
            result = {
                **self.values,
                "hitl_status": "approved",
                "hitl_version": 2,
                "applied_actions": {
                    "legacy-key": {"status": "approved", "version": 2}
                },
            }
        else:
            request = _request()
            result = {
                **self.values,
                "decision_ledger": {
                    self.decision.idempotency_key: _ledger(request, self.decision)
                },
                "decision_status": "current_authorization_denied",
                "pending_decision": None,
            }
        self.values = result
        return result


class _Container:
    def __init__(self, engine: _Engine, provider: _AuthorizationProvider) -> None:
        self.engine = engine
        self.provider = provider

    async def get_engine(self) -> _Engine:
        return self.engine

    def get_backend_authorization_provider(self) -> _AuthorizationProvider:
        return self.provider

    def authority_provenance(self) -> str:
        return "unavailable"


def _client(engine: _Engine) -> tuple[TestClient, _AuthorizationProvider]:
    app = FastAPI()
    provider = _AuthorizationProvider()
    app.state.container = _Container(engine, provider)
    register_v2_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id="alice", telephone=None, roles=["analyst"], permissions=["*"]
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    return TestClient(app), provider


def _typed_state() -> dict[str, object]:
    request = _request()
    return {
        "decision_status": "awaiting_decision",
        "decision_version": request.version,
        "pending_decision": request.model_dump(mode="json"),
        "decision_ledger": {},
    }


def test_missing_discriminator_preserves_legacy_action() -> None:
    engine = _Engine(
        {
            "hitl_status": "awaiting_action",
            "hitl_version": 1,
            "applied_actions": {},
        },
        None,
    )
    client, provider = _client(engine)
    response = client.post(
        f"/api/v2/nl2sql/threads/{_THREAD}/actions",
        json={
            "action": "approve",
            "idempotency_key": "legacy-key",
            "expected_version": 1,
        },
    )
    assert response.status_code == 200
    assert "target_type" not in response.json()
    assert response.json()["status"] == "approved"
    assert provider.calls == 1


def test_typed_decision_bypasses_legacy_gate_and_safe_continuation_is_200() -> None:
    decision = _decision()
    engine = _Engine(_typed_state(), decision)
    client, provider = _client(engine)
    response = client.post(
        f"/api/v2/nl2sql/threads/{_THREAD}/actions",
        json={
            "target_type": "typed_decision",
            "expected_version": 1,
            "decision": decision.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "thread_id": str(_THREAD),
        "target_type": "typed_decision",
        "status": "recorded",
        "version": 1,
        "idempotent": False,
        "decision_status": "resolved_pending_revalidation",
        "continuation_status": "current_authorization_denied",
    }
    assert engine.invocations == 1
    assert provider.calls == 1


def test_typed_stale_version_wrong_request_and_authority_field_fail() -> None:
    decision = _decision()
    engine = _Engine(_typed_state(), decision)
    client, _ = _client(engine)
    path = f"/api/v2/nl2sql/threads/{_THREAD}/actions"

    stale = client.post(
        path,
        json={
            "target_type": "typed_decision",
            "expected_version": 2,
            "decision": decision.model_dump(mode="json"),
        },
    )
    assert stale.status_code == 409

    wrong = _decision(request_id="clarify-other")
    wrong_response = client.post(
        path,
        json={
            "target_type": "typed_decision",
            "expected_version": 1,
            "decision": wrong.model_dump(mode="json"),
        },
    )
    assert wrong_response.status_code == 409

    payload = decision.model_dump(mode="json")
    payload["authorization"] = {"agent_enabled": True}
    authority = client.post(
        path,
        json={
            "target_type": "typed_decision",
            "expected_version": 1,
            "decision": payload,
        },
    )
    assert authority.status_code == 422


def test_typed_idempotent_exact_replay_and_conflict() -> None:
    decision = _decision()
    engine = _Engine(_typed_state(), decision)
    client, _ = _client(engine)
    path = f"/api/v2/nl2sql/threads/{_THREAD}/actions"
    body = {
        "target_type": "typed_decision",
        "expected_version": 1,
        "decision": decision.model_dump(mode="json"),
    }
    assert client.post(path, json=body).status_code == 200

    replay = client.post(path, json=body)
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True
    assert engine.invocations == 1

    different = _decision(value="2026-10")
    conflict = client.post(
        path,
        json={
            "target_type": "typed_decision",
            "expected_version": 1,
            "decision": different.model_dump(mode="json"),
        },
    )
    assert conflict.status_code == 409
