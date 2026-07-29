"""FastAPI 鉴权依赖。"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache

from fastapi import Depends, HTTPException
from starlette.requests import Request

from src.core.auth.provider import AuthError, TTApiAuthProvider
from src.core.auth.types import AuthUser
from src.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_V2_NL2SQL_PREFIX = "/api/v2/nl2sql"


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


def _required_nl2sql_permission(path: str, settings: Settings) -> str | None:
    """Resolve the permission for every authenticated NL2SQL v2 route.

    Returning ``None`` is intentional: the caller treats an unmapped route as
    a policy configuration error and fails closed. This prevents newly added
    endpoints from silently inheriting the previous empty-permission behavior.
    """

    normalized_path = path.rstrip("/") or "/"
    if normalized_path == f"{_V2_NL2SQL_PREFIX}/queries/stream":
        return settings.auth_required_permission_stream
    if normalized_path == f"{_V2_NL2SQL_PREFIX}/queries":
        return settings.auth_required_permission_invoke
    if normalized_path.startswith(f"{_V2_NL2SQL_PREFIX}/threads/"):
        return settings.auth_required_permission_invoke
    if normalized_path in {
        f"{_V2_NL2SQL_PREFIX}/feedback",
        f"{_V2_NL2SQL_PREFIX}/capabilities",
    }:
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


async def require_user(
    request: Request,
    provider: TTApiAuthProvider = Depends(get_auth_provider),
    settings: Settings = Depends(get_settings),
) -> AuthUser:
    if not settings.auth_enabled:
        return AuthUser(
            user_id="auth_disabled",
            telephone=None,
            roles=["system"],
            permissions=["*"],
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
    permission = _required_nl2sql_permission(path, settings)
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
