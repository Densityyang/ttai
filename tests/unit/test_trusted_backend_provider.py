"""Unit tests for the trusted Backend authorization provider."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.core.auth.backend_provider import (
    TrustedBackendAuthorizationProvider,
    bind_request_bearer_token,
    extract_bearer_token,
)
from src.core.auth.types import AuthUser
from src.core.settings import Settings
from src.nl2sql.contracts import AuthorizationContext


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        tt_api_base_url="http://tt-api:9000",
    )


def _user() -> AuthUser:
    return AuthUser(
        user_id="u-1",
        telephone=None,
        roles=[],
        permissions=[],
    )


class _StubResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _StubClient:
    def __init__(self, response: _StubResponse | Exception) -> None:
        self._response = response
        self.requests: list[dict[str, Any]] = []

    async def get(self, path: str, headers: dict[str, str]) -> _StubResponse:
        self.requests.append({"path": path, "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture(autouse=True)
def _clear_token() -> Any:
    bind_request_bearer_token(None)
    yield
    bind_request_bearer_token(None)


def test_extract_bearer_token_parses_and_rejects() -> None:
    assert extract_bearer_token("Bearer abc.def") == "abc.def"
    assert extract_bearer_token("bearer abc") == "abc"
    assert extract_bearer_token("Basic abc") is None
    assert extract_bearer_token("Bearer ") is None
    assert extract_bearer_token(None) is None


@pytest.mark.asyncio
async def test_load_without_token_fails_closed() -> None:
    client = _StubClient(_StubResponse(200, {}))
    provider = TrustedBackendAuthorizationProvider(settings=_settings(), client=client)
    assert await provider.load(_user()) is None
    assert client.requests == []


@pytest.mark.asyncio
async def test_load_maps_backend_payload_verbatim() -> None:
    payload = {
        "data": {
            "authorization_revision": "backend:rev-1",
            "agent_enabled": True,
            "scope_level": "team",
            "allowed_scope_ids": [101, 102],
        }
    }
    client = _StubClient(_StubResponse(200, payload))
    provider = TrustedBackendAuthorizationProvider(settings=_settings(), client=client)
    bind_request_bearer_token("token-1")

    context = await provider.load(_user())

    assert isinstance(context, AuthorizationContext)
    assert context.authorization_revision == "backend:rev-1"
    assert context.agent_enabled is True
    assert context.scope_level == "team"
    assert context.allowed_scope_ids == ("101", "102")
    request = client.requests[0]
    assert request["path"] == "/vadmin/auth/agent/authorization"
    assert request["headers"]["Authorization"] == "Bearer token-1"


@pytest.mark.asyncio
async def test_load_tolerates_unwrapped_payload() -> None:
    payload = {
        "authorization_revision": "backend:rev-2",
        "agent_enabled": True,
        "scope_level": "city_company",
        "allowed_scope_ids": ["*"],
    }
    client = _StubClient(_StubResponse(200, payload))
    provider = TrustedBackendAuthorizationProvider(settings=_settings(), client=client)
    bind_request_bearer_token("token-1")

    context = await provider.load(_user())

    assert context is not None
    assert context.scope_level == "city_company"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": "not-a-dict"},
        {"data": {"authorization_revision": "", "agent_enabled": True,
                  "scope_level": "team", "allowed_scope_ids": ["1"]}},
        {"data": {"authorization_revision": "r", "agent_enabled": "yes",
                  "scope_level": "team", "allowed_scope_ids": ["1"]}},
        {"data": {"authorization_revision": "r", "agent_enabled": True,
                  "scope_level": "province", "allowed_scope_ids": ["1"]}},
        {"data": {"authorization_revision": "r", "agent_enabled": True,
                  "scope_level": "team", "allowed_scope_ids": "1"}},
        {"data": {"authorization_revision": "r", "agent_enabled": True,
                  "scope_level": "team", "allowed_scope_ids": ["1", "1"]}},
        {"data": {"authorization_revision": "r", "agent_enabled": True,
                  "scope_level": "team", "allowed_scope_ids": [""]}},
        {"data": {"authorization_revision": "r", "agent_enabled": True,
                  "scope_level": "team"}},
    ],
)
async def test_load_malformed_payload_fails_closed(payload: Any) -> None:
    client = _StubClient(_StubResponse(200, payload))
    provider = TrustedBackendAuthorizationProvider(settings=_settings(), client=client)
    bind_request_bearer_token("token-1")
    assert await provider.load(_user()) is None


@pytest.mark.asyncio
async def test_load_non_200_fails_closed() -> None:
    client = _StubClient(_StubResponse(403, {}))
    provider = TrustedBackendAuthorizationProvider(settings=_settings(), client=client)
    bind_request_bearer_token("token-1")
    assert await provider.load(_user()) is None


@pytest.mark.asyncio
async def test_load_transport_error_fails_closed() -> None:
    client = _StubClient(httpx.ConnectError("boom"))
    provider = TrustedBackendAuthorizationProvider(settings=_settings(), client=client)
    bind_request_bearer_token("token-1")
    assert await provider.load(_user()) is None


@pytest.mark.asyncio
async def test_load_non_json_fails_closed() -> None:
    client = _StubClient(_StubResponse(200, ValueError("no json")))
    provider = TrustedBackendAuthorizationProvider(settings=_settings(), client=client)
    bind_request_bearer_token("token-1")
    assert await provider.load(_user()) is None


def test_provider_structurally_satisfies_backend_provider_protocol() -> None:
    # BackendAuthorizationProvider is a plain (non-runtime-checkable) Protocol:
    # conformance is structural, proven by the load() call surface used above.
    provider = TrustedBackendAuthorizationProvider(settings=_settings())
    assert callable(getattr(provider, "load", None))
