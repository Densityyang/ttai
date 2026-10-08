from __future__ import annotations

from collections.abc import Iterable
from typing import cast

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.core.auth.dependencies import (
    _required_nl2sql_permission,
    require_nl2sql_permission,
)
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
        _env_file=None,  # pyright: ignore[reportCallIssue]
        auth_required_permission_invoke="nl2sql:invoke",
        auth_required_permission_stream="nl2sql:stream",
    )


def _detail_code(exc: HTTPException) -> str:
    detail = exc.detail
    assert isinstance(detail, dict)
    return str(cast(dict[str, object], detail)["code"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v2/nl2sql/queries"),
        ("GET", "/api/v2/nl2sql/threads/11111111-1111-1111-1111-111111111111"),
        ("GET", "/api/v2/nl2sql/threads/11111111-1111-1111-1111-111111111111/history"),
        ("POST", "/api/v2/nl2sql/threads/11111111-1111-1111-1111-111111111111/actions"),
        ("POST", "/api/v2/nl2sql/feedback"),
        ("GET", "/api/v2/nl2sql/capabilities"),
    ],
)
async def test_non_stream_v2_routes_require_invoke_permission(method: str, path: str) -> None:
    request = _request(path, method=method)

    with pytest.raises(HTTPException) as denied:
        await require_nl2sql_permission(request, _user([]), _settings())
    assert denied.value.status_code == 403
    assert _detail_code(denied.value) == "AUTH_FORBIDDEN"

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
    assert _detail_code(denied.value) == "AUTH_FORBIDDEN"

    user = _user(["nl2sql:stream"])
    assert await require_nl2sql_permission(request, user, _settings()) is user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v2/nl2sql/future-endpoint"),
        ("POST", "/api/v2/nl2sql/threads/11111111-1111-1111-1111-111111111111/admin"),
        ("DELETE", "/api/v2/nl2sql/threads/11111111-1111-1111-1111-111111111111"),
    ],
)
async def test_unmapped_v2_route_fails_closed(method: str, path: str) -> None:
    request = _request(path, method=method)

    with pytest.raises(HTTPException) as denied:
        await require_nl2sql_permission(request, _user(["*"]), _settings())

    assert denied.value.status_code == 403
    assert _detail_code(denied.value) == "AUTH_PERMISSION_POLICY_MISSING"


@pytest.mark.asyncio
async def test_blank_permission_configuration_fails_closed() -> None:
    request = _request("/api/v2/nl2sql/queries", method="POST")
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        auth_required_permission_invoke=" ",
        auth_required_permission_stream="nl2sql:stream",
    )

    with pytest.raises(HTTPException) as denied:
        await require_nl2sql_permission(request, _user(["*"]), settings)

    assert denied.value.status_code == 403
    assert _detail_code(denied.value) == "AUTH_PERMISSION_POLICY_MISSING"

_DEF = "def_" + "a" * 32

PRODUCT_ENDPOINTS: tuple[tuple[str, str], ...] = (
    # 10 Definition / Publication endpoints
    ("POST", "/api/v2/nl2sql/definitions"),
    ("GET", "/api/v2/nl2sql/definitions"),
    ("GET", f"/api/v2/nl2sql/definitions/{_DEF}/versions/1"),
    ("PATCH", f"/api/v2/nl2sql/definitions/{_DEF}/draft"),
    ("POST", f"/api/v2/nl2sql/definitions/{_DEF}/semantic-close"),
    ("POST", f"/api/v2/nl2sql/definitions/{_DEF}/confirm"),
    ("POST", f"/api/v2/nl2sql/definitions/{_DEF}/save"),
    ("POST", f"/api/v2/nl2sql/definitions/{_DEF}/revisions"),
    ("POST", f"/api/v2/nl2sql/definitions/{_DEF}/versions/1/publish"),
    ("POST", f"/api/v2/nl2sql/definitions/{_DEF}/versions/1/execute"),
    # 11 Library endpoints
    ("GET", "/api/v2/nl2sql/library"),
    ("GET", "/api/v2/nl2sql/library/catalogue"),
    ("POST", "/api/v2/nl2sql/library/install"),
    ("POST", "/api/v2/nl2sql/library/uninstall"),
    ("POST", "/api/v2/nl2sql/library/star"),
    ("POST", "/api/v2/nl2sql/library/unstar"),
    ("POST", "/api/v2/nl2sql/library/upgrade"),
    ("POST", "/api/v2/nl2sql/library/acknowledge-withdrawal"),
    ("POST", "/api/v2/nl2sql/library/fork"),
    ("POST", "/api/v2/nl2sql/library/certify"),
    ("POST", "/api/v2/nl2sql/library/withdraw"),
    # 2 Personal conflict endpoints
    ("GET", "/api/v2/nl2sql/conflicts/personal"),
    ("POST", "/api/v2/nl2sql/conflicts/personal/select"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), PRODUCT_ENDPOINTS)
async def test_every_product_endpoint_requires_invoke_permission(
    method: str, path: str
) -> None:
    """All 23 product endpoints must resolve to the SAME bounded permission."""

    request = _request(path, method=method)
    settings = _settings()
    assert (
        _required_nl2sql_permission(method, path, settings)
        == settings.auth_required_permission_invoke
    )

    with pytest.raises(HTTPException) as denied:
        await require_nl2sql_permission(request, _user([]), settings)
    assert denied.value.status_code == 403
    assert _detail_code(denied.value) == "AUTH_FORBIDDEN"

    user = _user(["nl2sql:invoke"])
    assert await require_nl2sql_permission(request, user, settings) is user


def test_the_product_surface_is_exactly_twenty_three_endpoints() -> None:
    """Guards against a route silently appearing or disappearing."""

    assert len(PRODUCT_ENDPOINTS) == 23
    assert len(set(PRODUCT_ENDPOINTS)) == 23


@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/nl2sql/definitions/random/unmapped/path",
        "/api/v2/nl2sql/definitions/def_" + "a" * 32 + "/versions/1/publish-extra",
        "/api/v2/nl2sql/library/unknown-action",
    ],
)
def test_unknown_product_like_path_still_fails_closed(path: str) -> None:
    assert _required_nl2sql_permission("GET", path, _settings()) is None


def test_definition_patch_is_mapped_explicitly() -> None:
    """PATCH had no precedent; it must be a literal entry, not a wildcard."""

    path = f"/api/v2/nl2sql/definitions/{_DEF}/draft"
    settings = _settings()
    assert (
        _required_nl2sql_permission("PATCH", path, settings)
        == settings.auth_required_permission_invoke
    )
    # a PATCH on any OTHER definition path is NOT covered by that entry
    assert (
        _required_nl2sql_permission("PATCH", f"/api/v2/nl2sql/definitions/{_DEF}", settings)
        is None
    )


def test_no_permission_entry_matches_across_path_segments() -> None:
    """An encoded separator must not let a path match a mapped pattern."""

    settings = _settings()
    assert (
        _required_nl2sql_permission(
            "POST", "/api/v2/nl2sql/library/install/extra", settings
        )
        is None
    )
