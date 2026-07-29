from __future__ import annotations

from collections.abc import Iterable

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.core.auth.dependencies import require_nl2sql_permission
from src.core.auth.types import AuthUser
from src.core.settings import Settings


def _request(path: str, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
    )


def _user(permissions: Iterable[str]) -> AuthUser:
    return AuthUser(
        user_id="alice",
        telephone=None,
        roles=["analyst"],
        permissions=list(permissions),
    )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        auth_required_permission_invoke="nl2sql:invoke",
        auth_required_permission_stream="nl2sql:stream",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/nl2sql/queries",
        "/api/v2/nl2sql/threads/11111111-1111-1111-1111-111111111111",
        "/api/v2/nl2sql/threads/11111111-1111-1111-1111-111111111111/history",
        "/api/v2/nl2sql/threads/11111111-1111-1111-1111-111111111111/actions",
        "/api/v2/nl2sql/feedback",
        "/api/v2/nl2sql/capabilities",
    ],
)
async def test_non_stream_v2_routes_require_invoke_permission(path: str) -> None:
    request = _request(path)

    with pytest.raises(HTTPException) as denied:
        await require_nl2sql_permission(request, _user([]), _settings())
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "AUTH_FORBIDDEN"

    user = _user(["nl2sql:invoke"])
    assert await require_nl2sql_permission(request, user, _settings()) is user
    assert request.state.auth_user is user


@pytest.mark.asyncio
async def test_stream_v2_route_requires_stream_permission() -> None:
    request = _request("/api/v2/nl2sql/queries/stream", method="POST")

    with pytest.raises(HTTPException) as denied:
        await require_nl2sql_permission(
            request,
            _user(["nl2sql:invoke"]),
            _settings(),
        )
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "AUTH_FORBIDDEN"

    user = _user(["nl2sql:stream"])
    assert await require_nl2sql_permission(request, user, _settings()) is user


@pytest.mark.asyncio
async def test_unmapped_v2_route_fails_closed() -> None:
    request = _request("/api/v2/nl2sql/future-endpoint")

    with pytest.raises(HTTPException) as denied:
        await require_nl2sql_permission(request, _user(["*"]), _settings())

    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "AUTH_PERMISSION_POLICY_MISSING"


@pytest.mark.asyncio
async def test_blank_permission_configuration_fails_closed() -> None:
    request = _request("/api/v2/nl2sql/queries", method="POST")
    settings = Settings(
        _env_file=None,
        auth_required_permission_invoke=" ",
        auth_required_permission_stream="nl2sql:stream",
    )

    with pytest.raises(HTTPException) as denied:
        await require_nl2sql_permission(request, _user(["*"]), settings)

    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "AUTH_PERMISSION_POLICY_MISSING"
