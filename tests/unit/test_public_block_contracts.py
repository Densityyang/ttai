from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.nl2sql.api import extract_blocks
from src.nl2sql.orchestration.decision_contract import HITLRequest
from src.nl2sql.supervisor.schemas import (
    PUBLIC_BLOCK_MODELS,
    serialize_response_blocks,
)
from src.nl2sql.v2 import (
    BLOCK_MANIFEST,
    UNSUPPORTED_BLOCK_FALLBACK,
    register_v2_routes,
)


def test_manifest_exactly_matches_server_public_block_contracts() -> None:
    assert set(BLOCK_MANIFEST) == set(PUBLIC_BLOCK_MODELS)
    for block_type, model in PUBLIC_BLOCK_MODELS.items():
        schema = model.model_json_schema()
        assert schema["properties"]["type"]["const"] == block_type
        assert schema["additionalProperties"] is False


def test_unknown_raw_block_uses_visible_unsupported_fallback() -> None:
    serialized = serialize_response_blocks(
        [{"type": "future_visual", "private_payload": "must-not-pass"}]
    )
    assert serialized == [
        {
            "type": "future_visual",
            "fallback": dict(UNSUPPORTED_BLOCK_FALLBACK),
        }
    ]
    assert "private_payload" not in serialized[0]


def test_pending_typed_request_projects_only_safe_clarification_fields() -> None:
    request = HITLRequest(
        request_id="clarify-safe",
        decision_kind="clarification",
        version=2,
        allowed_actions=("resolve", "reject"),
        plan_sha256="a" * 64,
        context_checksum="b" * 64,
        policy_version="policy-v1",
        policy_checksum="c" * 64,
        issue_codes=("time_missing",),
        unresolved_slots=("time",),
        safe_summary="Choose a time range.",
    )

    blocks = extract_blocks(
        {
            "pending_decision": request.model_dump(mode="json"),
            "messages": [AIMessage(content="fallback must not win")],
        }
    )

    assert blocks == [
        {
            "type": "clarification",
            "request_id": "clarify-safe",
            "decision_kind": "clarification",
            "version": 2,
            "allowed_actions": ("resolve", "reject"),
            "unresolved_slots": ("time",),
            "issue_codes": ("time_missing",),
            "safe_summary": "Choose a time range.",
        }
    ]
    serialized = str(blocks).lower()
    for forbidden in ("authorization", "sql", "credential", "resume_token"):
        assert forbidden not in serialized


def test_query_capability_suggestion_is_route_neutral() -> None:
    blocks = extract_blocks(
        {
            "mode_capability_outcome": {
                "run_id": "run-1",
                "effective_mode": "QUERY",
                "outcome": "cannot_resolve",
                "suggested_mode": "ANALYZE",
            },
            "run_envelope": {"run_id": "run-1"},
        }
    )
    assert blocks[0]["type"] == "mode_suggestion"
    assert "FAST" not in str(blocks[0]).upper()
    assert "route" not in str(blocks[0]).lower()


class _Engine:
    async def ainvoke(self, *_: object, **__: object) -> dict[str, object]:
        return {"messages": [AIMessage(content="controlled response")]}


class _LocalRealContainer:
    async def get_engine(self) -> _Engine:
        return _Engine()

    def authority_provenance(self) -> str:
        return "local_real_demo"


def test_query_response_preserves_local_real_authority_provenance(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SERVICE_MODE", "infra-dev")
    from src.core.settings import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    app.state.container = _LocalRealContainer()
    register_v2_routes(app)

    async def identity() -> AuthUser:
        return AuthUser(
            user_id="local-real-user",
            telephone=None,
            roles=["demo"],
            permissions=["*"],
        )

    app.dependency_overrides[require_nl2sql_permission] = identity
    with TestClient(app) as client:
        response = client.post(
            "/api/v2/nl2sql/queries",
            json={
                "thread_id": str(uuid4()),
                "requested_mode": "QUERY",
                "messages": [{"role": "user", "content": "controlled"}],
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["authority_provenance"] == "local_real_demo"
    get_settings.cache_clear()
