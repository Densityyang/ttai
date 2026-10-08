"""FastAPI 鉴权依赖。"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache

from fastapi import Depends, HTTPException
from starlette.requests import Request

from src.core.auth.provider import AuthError, TTApiAuthProvider
from src.core.auth.types import AuthUser, OrganizationIdentity
from src.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_V2_NL2SQL_PREFIX = "/api/v2/nl2sql"
_THREAD_INVOKE_ROUTES = (
    ("GET", re.compile(rf"^{_V2_NL2SQL_PREFIX}/threads/[^/]+$")),
    ("GET", re.compile(rf"^{_V2_NL2SQL_PREFIX}/threads/[^/]+/history$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/threads/[^/]+/actions$")),
    # Definition resources.  NOTE: PATCH is absent from this tuple's history,
    # which is exactly why every method below is listed EXPLICITLY - an
    # unmapped route fails closed with 403 AUTH_PERMISSION_POLICY_MISSING.
    (
        "GET",
        re.compile(rf"^{_V2_NL2SQL_PREFIX}/definitions/[^/]+/versions/[0-9]+$"),
    ),
    ("PATCH", re.compile(rf"^{_V2_NL2SQL_PREFIX}/definitions/[^/]+/draft$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/definitions/[^/]+/semantic-close$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/definitions/[^/]+/confirm$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/definitions/[^/]+/save$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/definitions/[^/]+/revisions$")),
    (
        "POST",
        re.compile(rf"^{_V2_NL2SQL_PREFIX}/definitions/[^/]+/versions/[0-9]+/publish$"),
    ),
    (
        "POST",
        re.compile(rf"^{_V2_NL2SQL_PREFIX}/definitions/[^/]+/versions/[0-9]+/execute$"),
    ),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/library/install$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/library/uninstall$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/library/star$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/library/unstar$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/library/upgrade$")),
    (
        "POST",
        re.compile(rf"^{_V2_NL2SQL_PREFIX}/library/acknowledge-withdrawal$"),
    ),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/library/fork$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/library/certify$")),
    ("POST", re.compile(rf"^{_V2_NL2SQL_PREFIX}/library/withdraw$")),
)


def _get_trace_id(request: Request) -> str:
    return (
        request.headers.get("x-trace-id")
        or request.headers.get("x-request-id")
        or request.headers.get("traceparent")
        or "-"
    )


def _has_permission(user_permissions: list[str], required_permission: str) -> bool:
    if not required_permission:
        return True

    if required_permission in user_permissions:
        return True

    # tt-api 常见超级权限表达，表示全量权限。
    return "*" in user_permissions or "*.*.*" in user_permissions


def _required_nl2sql_permission(method: str, path: str, settings: Settings) -> str | None:
    """Resolve the permission for every authenticated NL2SQL v2 route.

    Returning ``None`` is intentional: the caller treats an unmapped route as
    a policy configuration error and fails closed. This prevents newly added
    endpoints from silently inheriting the previous empty-permission behavior.
    """

    route = (method.upper(), path.rstrip("/") or "/")
    if route == ("POST", f"{_V2_NL2SQL_PREFIX}/queries/stream"):
        return settings.auth_required_permission_stream
    if route in {
        ("POST", f"{_V2_NL2SQL_PREFIX}/queries"),
        ("POST", f"{_V2_NL2SQL_PREFIX}/feedback"),
        ("GET", f"{_V2_NL2SQL_PREFIX}/capabilities"),
        ("POST", f"{_V2_NL2SQL_PREFIX}/definitions"),
        ("GET", f"{_V2_NL2SQL_PREFIX}/definitions"),
        ("GET", f"{_V2_NL2SQL_PREFIX}/library"),
        ("GET", f"{_V2_NL2SQL_PREFIX}/library/catalogue"),
        ("GET", f"{_V2_NL2SQL_PREFIX}/conflicts/personal"),
        ("POST", f"{_V2_NL2SQL_PREFIX}/conflicts/personal/select"),
    }:
        return settings.auth_required_permission_invoke
    if any(
        route_method == route[0] and pattern.fullmatch(route[1])
        for route_method, pattern in _THREAD_INVOKE_ROUTES
    ):
        return settings.auth_required_permission_invoke
    return None


def _to_http_exception(error: AuthError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@lru_cache
def get_auth_provider() -> TTApiAuthProvider:
    return TTApiAuthProvider()


def _demo_identity_when_activated(settings: Settings) -> str | None:
    """The server-configured demo identity, ONLY under explicit demo activation.

    Returns None for every other deployment, so the ordinary auth-disabled
    identity is preserved.  The value comes from immutable Settings - never
    from a request body, query, header or cookie.
    """

    if settings.service_mode == "product":
        return None
    if settings.typed_runtime_activation == "demo_synthetic_authorization":
        return settings.demo_synthetic_user_id
    if settings.typed_runtime_activation == "local_real_data_demo":
        return settings.local_real_demo_user_id
    return None


async def require_user(
    request: Request,
    provider: TTApiAuthProvider = Depends(get_auth_provider),
    settings: Settings = Depends(get_settings),
) -> AuthUser:
    if not settings.auth_enabled:
        # EXPLICIT demo deployment: auth is disabled AND demo activation is on,
        # so the caller is the SERVER-CONFIGURED synthetic demo identity.  This
        # never changes the ordinary auth-disabled semantics below, and the
        # identity is never taken from the request.
        demo_user = _demo_identity_when_activated(settings)
        # An explicit demo/local identity gets ONLY the bounded NL2SQL
        # permissions this local application needs - never a "*" wildcard.
        # The historical auth_disabled path keeps its existing behavior.
        demo_permissions = (
            []
            if demo_user is None
            else sorted(
                {
                    settings.auth_required_permission_invoke,
                    settings.auth_required_permission_stream,
                }
            )
        )
        return AuthUser(
            user_id=demo_user or "auth_disabled",
            telephone=None,
            roles=["system"] if demo_user is None else ["demo"],
            permissions=["*"] if demo_user is None else demo_permissions,
            # With auth disabled there is NO Backend truth, so an all-None
            # identity is the honest value - distinct from "built without org
            # resolution" (which is organization=None).
            organization=OrganizationIdentity(),
        )

    started_at = time.perf_counter()
    trace_id = _get_trace_id(request)
    try:
        user = await provider.authenticate_request(request)
        logger.info(
            "auth verify success trace_id=%s user_id=%s method=%s path=%s permission_count=%d",
            trace_id,
            user.user_id,
            request.method,
            request.url.path,
            len(user.permissions),
        )
        return user
    except AuthError as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.warning(
            "auth verify failed trace_id=%s status_code=%s error_code=%s method=%s path=%s elapsed_ms=%.2f",
            trace_id,
            exc.status_code,
            exc.code,
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise _to_http_exception(exc) from exc


def require_permission(permission: str) -> Callable[..., Awaitable[AuthUser]]:
    async def _dependency(
        request: Request,
        user: AuthUser = Depends(require_user),
    ) -> AuthUser:
        if _has_permission(user.permissions, permission):
            return user

        trace_id = _get_trace_id(request)
        logger.warning(
            "auth permission denied trace_id=%s user_id=%s required_permission=%s method=%s path=%s",
            trace_id,
            user.user_id,
            permission,
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "AUTH_FORBIDDEN", "message": "无权限访问"},
        )

    return _dependency


async def require_nl2sql_permission(
    request: Request,
    user: AuthUser = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> AuthUser:
    path = request.url.path
    permission = _required_nl2sql_permission(request.method, path, settings)
    trace_id = _get_trace_id(request)

    if permission is None or not permission.strip():
        logger.error(
            "auth permission policy missing trace_id=%s user_id=%s method=%s path=%s",
            trace_id,
            user.user_id,
            request.method,
            path,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTH_PERMISSION_POLICY_MISSING",
                "message": "No access policy is configured for this endpoint",
            },
        )

    if _has_permission(user.permissions, permission):
        request.state.auth_user = user
        logger.info(
            "auth permission granted trace_id=%s user_id=%s required_permission=%s method=%s path=%s permission_count=%d",
            trace_id,
            user.user_id,
            permission,
            request.method,
            request.url.path,
            len(user.permissions),
        )
        return user

    logger.warning(
        "auth permission denied trace_id=%s user_id=%s required_permission=%s method=%s path=%s permission_count=%d",
        trace_id,
        user.user_id,
        permission,
        request.method,
        request.url.path,
        len(user.permissions),
    )
    raise HTTPException(
        status_code=403,
        detail={"code": "AUTH_FORBIDDEN", "message": "无权限访问"},
    )
